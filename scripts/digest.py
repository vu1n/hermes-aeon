"""Daily: format top scored items from the last 24h as a Telegram digest.

Delivery is handled by hermes cron's --deliver flag; this script just prints
the digest text to stdout (empty = silent, no message sent).
"""
from __future__ import annotations

import os
import sys
import time

from _common import setup_logging, get_db

log = setup_logging("digest")

LOOKBACK_HOURS = int(os.environ.get("DIGEST_LOOKBACK_HOURS", "24"))
TOP_N = int(os.environ.get("DIGEST_TOP_N", "8"))
MIN_SCORE = float(os.environ.get("DIGEST_MIN_SCORE", "0.65"))


def main() -> int:
    db = get_db()
    try:
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - LOOKBACK_HOURS * 3600 * 1000

        rows = db.execute(
            "SELECT title, url, summary, quality_score, source FROM memory_items "
            "WHERE captured_at >= ? AND quality_score IS NOT NULL AND quality_score >= ? "
            "AND source LIKE 'discover:%' "
            "ORDER BY quality_score DESC LIMIT ?",
            (cutoff_ms, MIN_SCORE, TOP_N),
        ).fetchall()

        if not rows:
            log.info("no items above threshold %.2f in last %dh", MIN_SCORE, LOOKBACK_HOURS)
            return 0  # empty stdout -> hermes cron sends nothing

        lines = [f"*Aeon digest* — top {len(rows)} from the last {LOOKBACK_HOURS}h\n"]
        for title, url, summary, score, source in rows:
            src_label = source.split(":", 1)[1] if ":" in source else source
            short_summary = (summary or "")[:160].rstrip()
            lines.append(f"• [{score:.2f} · {src_label}] [{title[:80]}]({url})")
            if short_summary:
                lines.append(f"   _{short_summary}_")

        print("\n".join(lines))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
