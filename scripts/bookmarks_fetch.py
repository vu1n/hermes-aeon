"""Hourly: pull new X bookmarks via xurl, capture each as a memory.

Bookmarks are the interest signal — captured uncritically (no scoring), with
source='x-bookmark'. The derive_profile.py script later summarizes recent
bookmarks into the interest profile.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from _common import setup_logging, get_db
from store import queries as q
from store.embed import embed_text

log = setup_logging("bookmarks_fetch")

X_USER_ID = os.environ.get("X_USER_ID", "15781023")
XURL_APP = os.environ.get("XURL_APP", "hermes-aeon")
MAX_RESULTS = int(os.environ.get("BOOKMARKS_MAX_RESULTS", "100"))
BACKFILL = "--backfill" in sys.argv


def call_xurl(path: str) -> dict:
    """Invoke xurl CLI, return parsed JSON. Auth lives in ~/.xurl, never read here."""
    cmd = ["xurl", "--app", XURL_APP, path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"xurl failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def latest_bookmark_id(db) -> str | None:
    """Return the highest tweet_id we've already captured, for since_id."""
    row = db.execute(
        "SELECT url FROM memory_items "
        "WHERE source = 'x-bookmark' AND url LIKE 'https://x.com/i/web/status/%' "
        "ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return row[0].rsplit("/", 1)[-1]


def capture_bookmark(db, tweet: dict) -> bool:
    """Capture one bookmark. Returns True if new (False if dedup-skipped)."""
    tid = tweet["id"]
    text = tweet.get("text", "")
    article = tweet.get("article") or {}
    title = article.get("title") or text[:120].replace("\n", " ").strip() or tid
    url = f"https://x.com/i/web/status/{tid}"

    emb, model = embed_text(text or title, provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))

    before = db.execute("SELECT COUNT(*) FROM memory_items WHERE dedup_key = ?",
                        (f"x-bookmark:{tid}",)).fetchone()[0]

    q.capture_memory(
        db, type="link", domain="learning",
        title=title, content=text, url=url,
        tags=["x-bookmark"],
        source="x-bookmark",
        dedup_key=f"x-bookmark:{tid}",
        embedding=emb, embedding_model=model,
    )

    after = db.execute("SELECT COUNT(*) FROM memory_items WHERE dedup_key = ?",
                       (f"x-bookmark:{tid}",)).fetchone()[0]
    return after > before


def main() -> int:
    db = get_db()
    try:
        path = f"/2/users/{X_USER_ID}/bookmarks?max_results={MAX_RESULTS}"
        if not BACKFILL:
            since = latest_bookmark_id(db)
            if since:
                path += f"&since_id={since}"

        captured = 0
        seen = 0
        pages = 0
        next_token: str | None = None

        while True:
            page_path = path + (f"&pagination_token={next_token}" if next_token else "")
            data = call_xurl(page_path)
            pages += 1
            for tweet in data.get("data") or []:
                seen += 1
                if capture_bookmark(db, tweet):
                    captured += 1
            meta = data.get("meta") or {}
            next_token = meta.get("next_token")
            # Only paginate in backfill mode; in incremental mode the first page covers us.
            if not next_token or not BACKFILL:
                break

        log.info("pages=%d seen=%d captured=%d backfill=%s",
                 pages, seen, captured, BACKFILL)
        if captured:
            print(f"captured {captured} new bookmarks")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
