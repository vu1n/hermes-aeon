"""Pull new X bookmarks via xurl. Returns dict {captured, seen, pages, backfill}."""
from __future__ import annotations

import json
import logging
import os
import subprocess

from store import queries as q
from store.embed import embed_text

log = logging.getLogger("aeon.ingest.bookmarks")


def _xurl_get(path: str, app: str) -> dict:
    proc = subprocess.run(
        ["xurl", "--app", app, path],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"xurl failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def _latest_id(db) -> str | None:
    row = db.execute(
        "SELECT url FROM memory_items "
        "WHERE source = 'x-bookmark' AND url LIKE 'https://x.com/i/web/status/%' "
        "ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()
    return row[0].rsplit("/", 1)[-1] if row else None


def _capture(db, tweet: dict) -> bool:
    tid = tweet["id"]
    text = tweet.get("text", "")
    article = tweet.get("article") or {}
    title = article.get("title") or text[:120].replace("\n", " ").strip() or tid
    url = f"https://x.com/i/web/status/{tid}"
    emb, model = embed_text(text or title, provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))

    dedup = f"x-bookmark:{tid}"
    before = db.execute("SELECT COUNT(*) FROM memory_items WHERE dedup_key = ?",
                        (dedup,)).fetchone()[0]
    q.capture_memory(
        db, type="link", domain="learning",
        title=title, content=text, url=url,
        tags=["x-bookmark"],
        source="x-bookmark",
        dedup_key=dedup,
        embedding=emb, embedding_model=model,
    )
    after = db.execute("SELECT COUNT(*) FROM memory_items WHERE dedup_key = ?",
                       (dedup,)).fetchone()[0]
    return after > before


def run(db, *, backfill: bool = False, max_results: int = 100) -> dict:
    user_id = os.environ.get("X_USER_ID", "15781023")
    app = os.environ.get("XURL_APP", "hermes-aeon")

    path = f"/2/users/{user_id}/bookmarks?max_results={max_results}"
    if not backfill:
        since = _latest_id(db)
        if since:
            path += f"&since_id={since}"

    captured = seen = pages = 0
    next_token: str | None = None

    while True:
        page_path = path + (f"&pagination_token={next_token}" if next_token else "")
        data = _xurl_get(page_path, app)
        pages += 1
        for tweet in data.get("data") or []:
            seen += 1
            if _capture(db, tweet):
                captured += 1
        next_token = (data.get("meta") or {}).get("next_token")
        if not next_token or not backfill:
            break

    log.info("pages=%d seen=%d captured=%d backfill=%s", pages, seen, captured, backfill)
    return {"captured": captured, "seen": seen, "pages": pages, "backfill": backfill}
