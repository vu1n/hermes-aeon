"""Daily: pull Oura daily summaries (sleep, activity, readiness, workouts).

One memory_item per (category, date) so aeon can correlate across categories
on the same day. captured_at = midnight UTC of the day the data refers to,
NOT the cron run time — so digest queries / 'last 7 days' searches behave
sensibly.

Incremental run pulls the last N days (default 3) and lets dedup_key skip
already-captured days. --backfill <N> overrides the window.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from _common import setup_logging, get_db
from store import queries as q
from store.embed import embed_text

log = setup_logging("oura_fetch")

OURA_PAT = os.environ.get("OURA_PAT")
OURA_API = "https://api.ouraring.com/v2/usercollection"
INCREMENTAL_DAYS = int(os.environ.get("OURA_INCREMENTAL_DAYS", "3"))
BACKFILL_DAYS = int(os.environ.get("OURA_BACKFILL_DAYS", "30"))

BACKFILL = "--backfill" in sys.argv


def _headers() -> dict:
    if not OURA_PAT:
        raise RuntimeError("OURA_PAT not set")
    return {"Authorization": f"Bearer {OURA_PAT}"}


def fetch_window(endpoint: str, start: str, end: str) -> list[dict]:
    """GET /v2/usercollection/<endpoint>?start_date=&end_date= (paginated)."""
    items: list[dict] = []
    next_token: Optional[str] = None
    while True:
        params = {"start_date": start, "end_date": end}
        if next_token:
            params["next_token"] = next_token
        resp = httpx.get(f"{OURA_API}/{endpoint}", headers=_headers(),
                         params=params, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("data") or [])
        next_token = data.get("next_token")
        if not next_token:
            break
    return items


def day_ts_ms(day_str: str) -> int:
    """Convert YYYY-MM-DD to midnight UTC ms."""
    return int(datetime.fromisoformat(day_str).replace(tzinfo=timezone.utc).timestamp() * 1000)


def fmt_minutes(seconds: Optional[int]) -> str:
    if not seconds:
        return "?"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m"


def render_sleep(d: dict) -> tuple[str, str]:
    day = d.get("day", "?")
    score = d.get("score")
    contributors = d.get("contributors") or {}
    total = d.get("total_sleep_duration") or 0
    deep = d.get("deep_sleep_duration") or 0
    rem = d.get("rem_sleep_duration") or 0
    title = f"Sleep {day}: score {score} · total {fmt_minutes(total)}"
    content = "\n".join([
        f"date: {day}",
        f"score: {score}",
        f"total_sleep: {fmt_minutes(total)}",
        f"deep: {fmt_minutes(deep)}",
        f"rem: {fmt_minutes(rem)}",
        f"contributors: {contributors}",
    ])
    return title, content


def render_activity(d: dict) -> tuple[str, str]:
    day = d.get("day", "?")
    score = d.get("score")
    steps = d.get("steps")
    active_cal = d.get("active_calories")
    total_cal = d.get("total_calories")
    title = f"Activity {day}: score {score} · steps {steps}"
    content = "\n".join([
        f"date: {day}",
        f"score: {score}",
        f"steps: {steps}",
        f"active_calories: {active_cal}",
        f"total_calories: {total_cal}",
        f"target_calories: {d.get('target_calories')}",
        f"high_activity_time: {fmt_minutes(d.get('high_activity_time') or 0)}",
        f"medium_activity_time: {fmt_minutes(d.get('medium_activity_time') or 0)}",
        f"low_activity_time: {fmt_minutes(d.get('low_activity_time') or 0)}",
        f"sedentary_time: {fmt_minutes(d.get('sedentary_time') or 0)}",
    ])
    return title, content


def render_readiness(d: dict) -> tuple[str, str]:
    day = d.get("day", "?")
    score = d.get("score")
    contributors = d.get("contributors") or {}
    title = f"Readiness {day}: score {score}"
    content = "\n".join([
        f"date: {day}",
        f"score: {score}",
        f"temperature_deviation: {d.get('temperature_deviation')}",
        f"temperature_trend_deviation: {d.get('temperature_trend_deviation')}",
        f"contributors: {contributors}",
    ])
    return title, content


def render_workout(d: dict) -> tuple[str, str]:
    day = d.get("day", "?")
    activity = d.get("activity", "?")
    intensity = d.get("intensity", "?")
    cal = d.get("calories")
    dist = d.get("distance")
    title = f"Workout {day}: {activity} · {intensity} · {cal}cal"
    content = "\n".join([
        f"date: {day}",
        f"activity: {activity}",
        f"intensity: {intensity}",
        f"calories: {cal}",
        f"distance_m: {dist}",
        f"start_datetime: {d.get('start_datetime')}",
        f"end_datetime: {d.get('end_datetime')}",
        f"label: {d.get('label')}",
    ])
    return title, content


CATEGORIES = [
    ("daily_sleep",     "sleep",      render_sleep),
    ("daily_activity",  "activity",   render_activity),
    ("daily_readiness", "readiness",  render_readiness),
    ("workout",         "workout",    render_workout),
]


def main() -> int:
    if not OURA_PAT:
        log.error("OURA_PAT not set")
        return 2

    days = BACKFILL_DAYS if BACKFILL else INCREMENTAL_DAYS
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()
    log.info("window: %s -> %s (%s)", start, end, "BACKFILL" if BACKFILL else "incremental")

    db = get_db()
    try:
        total_captured = 0
        for endpoint, label, renderer in CATEGORIES:
            try:
                rows = fetch_window(endpoint, start, end)
            except Exception as e:
                log.warning("%s fetch failed: %s", endpoint, e)
                continue

            captured = 0
            for d in rows:
                day = d.get("day")
                if not day:
                    continue
                # Workouts can have multiple per day — disambiguate by start time
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
                    captured_at=day_ts_ms(day),
                    embedding=emb, embedding_model=model,
                )
                captured += 1

            log.info("%s: rows=%d captured=%d", label, len(rows), captured)
            total_captured += captured

        if total_captured:
            print(f"oura: captured {total_captured} items ({days}-day window)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
