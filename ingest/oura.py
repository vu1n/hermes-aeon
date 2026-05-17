"""Pull Oura daily summaries. Returns dict {captured, per_category, days, backfill}."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from store import queries as q
from store.embed import embed_text

log = logging.getLogger("aeon.ingest.oura")

OURA_API = "https://api.ouraring.com/v2/usercollection"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _fetch_window(endpoint: str, start: str, end: str, token: str) -> list[dict]:
    items: list[dict] = []
    next_token: Optional[str] = None
    while True:
        params = {"start_date": start, "end_date": end}
        if next_token:
            params["next_token"] = next_token
        resp = httpx.get(f"{OURA_API}/{endpoint}", headers=_headers(token),
                         params=params, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("data") or [])
        next_token = data.get("next_token")
        if not next_token:
            break
    return items


def _day_ts_ms(day_str: str) -> int:
    return int(datetime.fromisoformat(day_str).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _fmt_minutes(seconds: Optional[int]) -> str:
    if not seconds:
        return "?"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m"


def _render_sleep(d: dict) -> tuple[str, str]:
    day = d.get("day", "?")
    score = d.get("score")
    contributors = d.get("contributors") or {}
    total = d.get("total_sleep_duration") or 0
    deep = d.get("deep_sleep_duration") or 0
    rem = d.get("rem_sleep_duration") or 0
    title = f"Sleep {day}: score {score}"
    if total:
        title += f" · total {_fmt_minutes(total)}"
    content = "\n".join([
        f"date: {day}", f"score: {score}",
        f"total_sleep: {_fmt_minutes(total)}",
        f"deep: {_fmt_minutes(deep)}",
        f"rem: {_fmt_minutes(rem)}",
        f"contributors: {contributors}",
    ])
    return title, content


def _render_activity(d: dict) -> tuple[str, str]:
    day = d.get("day", "?")
    title = f"Activity {day}: score {d.get('score')} · steps {d.get('steps')}"
    content = "\n".join([
        f"date: {day}", f"score: {d.get('score')}",
        f"steps: {d.get('steps')}",
        f"active_calories: {d.get('active_calories')}",
        f"total_calories: {d.get('total_calories')}",
        f"target_calories: {d.get('target_calories')}",
        f"high_activity_time: {_fmt_minutes(d.get('high_activity_time') or 0)}",
        f"medium_activity_time: {_fmt_minutes(d.get('medium_activity_time') or 0)}",
        f"low_activity_time: {_fmt_minutes(d.get('low_activity_time') or 0)}",
        f"sedentary_time: {_fmt_minutes(d.get('sedentary_time') or 0)}",
    ])
    return title, content


def _render_readiness(d: dict) -> tuple[str, str]:
    day = d.get("day", "?")
    title = f"Readiness {day}: score {d.get('score')}"
    content = "\n".join([
        f"date: {day}", f"score: {d.get('score')}",
        f"temperature_deviation: {d.get('temperature_deviation')}",
        f"temperature_trend_deviation: {d.get('temperature_trend_deviation')}",
        f"contributors: {d.get('contributors') or {}}",
    ])
    return title, content


def _render_workout(d: dict) -> tuple[str, str]:
    day = d.get("day", "?")
    title = f"Workout {day}: {d.get('activity', '?')} · {d.get('intensity', '?')} · {d.get('calories')}cal"
    content = "\n".join([
        f"date: {day}",
        f"activity: {d.get('activity')}", f"intensity: {d.get('intensity')}",
        f"calories: {d.get('calories')}", f"distance_m: {d.get('distance')}",
        f"start_datetime: {d.get('start_datetime')}",
        f"end_datetime: {d.get('end_datetime')}",
        f"label: {d.get('label')}",
    ])
    return title, content


CATEGORIES = [
    ("daily_sleep",     "sleep",     _render_sleep),
    ("daily_activity",  "activity",  _render_activity),
    ("daily_readiness", "readiness", _render_readiness),
    ("workout",         "workout",   _render_workout),
]


def run(db, *, backfill: bool = False, days: Optional[int] = None) -> dict:
    token = os.environ.get("OURA_PAT")
    if not token:
        raise RuntimeError("OURA_PAT not set")

    n_days = days or int(os.environ.get(
        "OURA_BACKFILL_DAYS" if backfill else "OURA_INCREMENTAL_DAYS",
        "30" if backfill else "3",
    ))
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=n_days)).isoformat()
    end = today.isoformat()
    log.info("window: %s -> %s (%s)", start, end, "BACKFILL" if backfill else "incremental")

    total_captured = 0
    per_category: dict[str, int] = {}
    for endpoint, label, renderer in CATEGORIES:
        try:
            rows = _fetch_window(endpoint, start, end, token)
        except Exception as e:
            log.warning("%s fetch failed: %s", endpoint, e)
            per_category[label] = 0
            continue

        captured = 0
        for d in rows:
            day = d.get("day")
            if not day:
                continue
            if label == "workout":
                dedup_id = f"{day}:{d.get('id') or d.get('start_datetime')}"
            else:
                dedup_id = day
            dedup_key = f"oura:{label}:{dedup_id}"
            if db.execute("SELECT 1 FROM memory_items WHERE dedup_key = ? LIMIT 1",
                          (dedup_key,)).fetchone():
                continue
            title, content = renderer(d)
            emb, model = embed_text(title + "\n" + content,
                                    provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))
            q.capture_memory(
                db, type="health_log", domain="health",
                title=title, content=content,
                tags=["oura", label],
                source=f"oura:{label}",
                dedup_key=dedup_key,
                captured_at=_day_ts_ms(day),
                embedding=emb, embedding_model=model,
            )
            captured += 1

        per_category[label] = captured
        total_captured += captured

    return {"captured": total_captured, "per_category": per_category,
            "days": n_days, "backfill": backfill}
