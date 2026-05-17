"""Daily: re-derive interest profile from recent X bookmarks.

Reads bookmarks captured in the last N days (default 90), weights them by
recency (linear decay), and asks the LLM to summarize the cross-cutting
themes into a 1-2 paragraph interest profile. The profile is a single
memory_item with dedup_key='profile:interests' — each run appends a revision,
so profile evolution is visible in memory_revisions.
"""
from __future__ import annotations

import os
import sys
import time

from _common import (
    setup_logging, get_db, llm_chat,
    get_profile_text, upsert_profile,
)

log = setup_logging("derive_profile")

LOOKBACK_DAYS = int(os.environ.get("PROFILE_LOOKBACK_DAYS", "90"))
MIN_BOOKMARKS = int(os.environ.get("PROFILE_MIN_BOOKMARKS", "5"))


SYSTEM = """You distill a person's interest profile from their X bookmarks.
Bookmarks shown to you are weighted by recency (recent ones matter more).

Output: 1–2 paragraphs of plain prose, no headers, no lists, no preamble.
Capture cross-cutting themes, not individual topics. Be specific about
*flavors* of interest (e.g., "agent runtimes and harness engineering" beats
"AI"; "open-source distributed systems" beats "infra"). Avoid vague filler
words. Write so the profile would help a relevance scorer judge whether a
new article fits this person's interests."""


def main() -> int:
    db = get_db()
    try:
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - LOOKBACK_DAYS * 86400 * 1000

        rows = db.execute(
            "SELECT title, content, captured_at FROM memory_items "
            "WHERE source = 'x-bookmark' AND captured_at >= ? "
            "ORDER BY captured_at DESC",
            (cutoff_ms,),
        ).fetchall()

        if len(rows) < MIN_BOOKMARKS:
            log.info("only %d bookmarks in last %d days (need %d); skipping",
                     len(rows), LOOKBACK_DAYS, MIN_BOOKMARKS)
            print(f"profile: too few bookmarks ({len(rows)}/{MIN_BOOKMARKS}); skipped")
            return 0

        # Recency weight: linear decay from 1.0 (newest) to 0.3 (cutoff)
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
            log.warning("LLM returned empty profile; skipping")
            print("profile: empty LLM response; skipped")
            return 1

        old_profile = get_profile_text(db) or ""
        if new_profile.strip() == old_profile.strip():
            log.info("profile unchanged (%d chars); skipping write", len(new_profile))
            print("profile: unchanged; no revision")
            return 0

        summary = new_profile.split(".")[0][:200] + "."
        mid = upsert_profile(db, content=new_profile, summary=summary)
        log.info("profile updated id=%s len=%d", mid, len(new_profile))
        print(f"profile updated ({len(rows)} bookmarks, {len(new_profile)} chars)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
