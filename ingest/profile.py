"""Re-derive interest profile from recent X bookmarks."""
from __future__ import annotations

import logging
import os
import time

from ._common import llm_chat, get_profile_text, upsert_profile

log = logging.getLogger("aeon.ingest.profile")


SYSTEM = """You distill a person's interest profile from their X bookmarks.
Bookmarks shown to you are weighted by recency (recent ones matter more).

Output: 1–2 paragraphs of plain prose, no headers, no lists, no preamble.
Capture cross-cutting themes, not individual topics. Be specific about
*flavors* of interest (e.g., "agent runtimes and harness engineering" beats
"AI"; "open-source distributed systems" beats "infra"). Avoid vague filler
words. Write so the profile would help a relevance scorer judge whether a
new article fits this person's interests."""


def run(db, *, lookback_days: int = 90, min_bookmarks: int = 5) -> dict:
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - lookback_days * 86400 * 1000

    rows = db.execute(
        "SELECT title, content, captured_at FROM memory_items "
        "WHERE source = 'x-bookmark' AND captured_at >= ? "
        "ORDER BY captured_at DESC",
        (cutoff_ms,),
    ).fetchall()

    if len(rows) < min_bookmarks:
        return {"updated": False, "bookmarks": len(rows),
                "reason": f"too_few_bookmarks ({len(rows)}/{min_bookmarks})"}

    oldest_ms = rows[-1][2]
    span_ms = max(1, now_ms - oldest_ms)
    lines: list[str] = []
    for title, content, ts in rows:
        weight = 0.3 + 0.7 * ((ts - oldest_ms) / span_ms)
        snippet = (content or title or "")[:300].replace("\n", " ").strip()
        lines.append(f"[w={weight:.2f}] {snippet}")

    prompt = (
        f"Bookmarks (most recent first, n={len(rows)}):\n\n"
        + "\n".join(lines)
        + "\n\nWrite the interest profile now."
    )

    new_profile = llm_chat(prompt, system=SYSTEM, max_tokens=800, temperature=0.3).strip()
    if not new_profile:
        return {"updated": False, "bookmarks": len(rows), "reason": "empty_llm_response"}

    old_profile = get_profile_text(db) or ""
    if new_profile.strip() == old_profile.strip():
        return {"updated": False, "bookmarks": len(rows), "reason": "unchanged",
                "profile_chars": len(new_profile)}

    summary = new_profile.split(".")[0][:200] + "."
    mid = upsert_profile(db, content=new_profile, summary=summary)
    log.info("profile updated id=%s len=%d (from %d bookmarks)", mid, len(new_profile), len(rows))
    return {"updated": True, "bookmarks": len(rows), "profile_chars": len(new_profile),
            "memory_id": mid}
