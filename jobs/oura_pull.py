"""Hermes cron script (no agent): pull Oura daily summaries into Aeon Emerald.

Each day and category is one source document (``oura:<category>:<day>``). Oura revises the current
day as it syncs, so each change is a new version; an unchanged day Emerald already acknowledged is not
sent again (see ``Ledger``), so a day the owner erased does not come back.
Prints a JSON summary; exits 1 if any category failed to fetch or any capture failed.

    oura_pull.py [--days N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from client import Emerald, EmeraldError, Ledger, capture_arguments, setting  # noqa: E402

OURA_API = "https://api.ouraring.com/v2/usercollection"
KEY_SETTING = "AEON_EMERALD_OURA_KEY"


def _minutes(seconds: Optional[int]) -> str:
    if not seconds:
        return "?"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m"


def render_sleep(d: dict) -> str:
    # daily_sleep carries the score and its contributors; durations are only on the /sleep periods.
    return "\n".join([f"Sleep {d.get('day')}: score {d.get('score')}", "",
                      f"date: {d.get('day')}", f"score: {d.get('score')}",
                      f"contributors: {d.get('contributors') or {}}"])


def render_activity(d: dict) -> str:
    return "\n".join([f"Activity {d.get('day')}: score {d.get('score')} · steps {d.get('steps')}", "",
                      f"date: {d.get('day')}", f"score: {d.get('score')}", f"steps: {d.get('steps')}",
                      f"active_calories: {d.get('active_calories')}",
                      f"total_calories: {d.get('total_calories')}",
                      f"high_activity_time: {_minutes(d.get('high_activity_time'))}",
                      f"medium_activity_time: {_minutes(d.get('medium_activity_time'))}",
                      f"low_activity_time: {_minutes(d.get('low_activity_time'))}",
                      f"sedentary_time: {_minutes(d.get('sedentary_time'))}"])


def render_readiness(d: dict) -> str:
    return "\n".join([f"Readiness {d.get('day')}: score {d.get('score')}", "",
                      f"date: {d.get('day')}", f"score: {d.get('score')}",
                      f"temperature_deviation: {d.get('temperature_deviation')}",
                      f"temperature_trend_deviation: {d.get('temperature_trend_deviation')}",
                      f"contributors: {d.get('contributors') or {}}"])


def render_workout(d: dict) -> str:
    return "\n".join([f"Workout {d.get('day')}: {d.get('activity', '?')} · {d.get('intensity', '?')} · "
                      f"{d.get('calories')}cal", "",
                      f"date: {d.get('day')}", f"activity: {d.get('activity')}",
                      f"intensity: {d.get('intensity')}", f"calories: {d.get('calories')}",
                      f"distance_m: {d.get('distance')}", f"start_datetime: {d.get('start_datetime')}",
                      f"end_datetime: {d.get('end_datetime')}", f"label: {d.get('label')}"])


CATEGORIES: list[tuple[str, str, Callable[[dict], str]]] = [
    ("daily_sleep", "sleep", render_sleep),
    ("daily_activity", "activity", render_activity),
    ("daily_readiness", "readiness", render_readiness),
    ("workout", "workout", render_workout),
]


def fetch(http: httpx.Client, endpoint: str, start: str, end: str) -> list[dict]:
    rows: list[dict] = []
    params: dict[str, str] = {"start_date": start, "end_date": end}
    while True:
        response = http.get(f"{OURA_API}/{endpoint}", params=params)
        response.raise_for_status()
        data = response.json()
        rows.extend(data.get("data") or [])
        if not data.get("next_token"):
            return rows
        params["next_token"] = data["next_token"]


def episode(label: str, row: dict, render: Callable[[dict], str]) -> Optional[dict[str, Any]]:
    """Capture arguments for one Oura row, or None for a row without a day."""
    day = row.get("day")
    if not day:
        return None
    if label == "workout":
        external_id = f"oura:workout:{day}:{row.get('id') or row.get('start_datetime')}"
        occurred_at = row.get("start_datetime") or f"{day}T00:00:00Z"
    else:
        external_id = f"oura:{label}:{day}"
        occurred_at = f"{day}T00:00:00Z"
    return capture_arguments(content=render(row), kind="text", external_id=external_id,
                             occurred_at=occurred_at, project="health", sensitivity=["health"])


def run(emerald: Emerald, oura: httpx.Client, days: int, ledger: Ledger,
        today: Optional[str] = None) -> dict[str, Any]:
    end = datetime.fromisoformat(today).date() if today else datetime.now(timezone.utc).date()
    start = (end - timedelta(days=days)).isoformat()
    summary: dict[str, Any] = {"window": [start, end.isoformat()], "sent": {}, "unchanged": 0, "errors": []}
    for endpoint, label, render in CATEGORIES:
        try:
            rows = fetch(oura, endpoint, start, end.isoformat())
        except httpx.HTTPError as error:
            summary["errors"].append(f"fetch {endpoint}: {type(error).__name__}: {error}")
            continue
        sent = 0
        for row in rows:
            arguments = episode(label, row, render)
            if arguments is None:
                continue
            if arguments["operation_id"] in ledger:
                summary["unchanged"] += 1
                continue
            try:
                emerald.capture(arguments)
            except EmeraldError as error:
                summary["errors"].append(f"capture {arguments['source']['external_id']}: {error}")
                continue
            ledger.add(arguments["operation_id"])
            sent += 1
        summary["sent"][label] = sent
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=3, help="days back from today to (re)send")
    args = parser.parse_args(argv)
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    emerald = Emerald.from_settings(home, KEY_SETTING)
    oura = httpx.Client(timeout=30.0, headers={"Authorization": f"Bearer {setting('OURA_PAT', home)}"})
    try:
        summary = run(emerald, oura, args.days, Ledger(home / "aeon-emerald" / "acknowledged-oura"))
    finally:
        emerald.close()
        oura.close()
    print(json.dumps(summary))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
