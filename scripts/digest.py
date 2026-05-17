"""Daily: synthesize a readout across all sources (discoveries + work + body + bookmarks).

Old shape was a bullet list with truncated per-item summaries. New shape is an
LLM-synthesized narrative: 1-line opening + sections (research/work/body/worth
your time) + brief prose with inline [title](url) links rather than dumped
items. Aligns with the 'synthesize, don't summarize' principle from depeche PRD.

Inputs:
- discoveries:    last 24h with quality_score (HN, Lobste.rs, X, HF papers)
- github events:  last 24h (commits, PRs, reviews, comments, releases, stars)
- oura summaries: yesterday's sleep/activity/readiness
- recent bookmarks: last 24h (the interest-evolution signal)

Output: Telegram-friendly markdown to stdout. Empty stdout = silent (no message).
"""
from __future__ import annotations

import os
import sys
import time

from _common import setup_logging, get_db, llm_chat

log = setup_logging("digest")

LOOKBACK_HOURS = int(os.environ.get("DIGEST_LOOKBACK_HOURS", "24"))
MAX_DISCOVERIES = int(os.environ.get("DIGEST_MAX_DISCOVERIES", "20"))
MAX_GITHUB = int(os.environ.get("DIGEST_MAX_GITHUB", "20"))
MAX_BOOKMARKS = int(os.environ.get("DIGEST_MAX_BOOKMARKS", "10"))
MIN_SCORE = float(os.environ.get("DIGEST_MIN_SCORE", "0.6"))


SYSTEM = """You are Aeon, writing the user's daily readout.

Voice: direct, capability-focused. No filler. No headers like 'Daily Digest' or
date stamps — write so a person reading on their phone gets the signal fast.
Telegram markdown only (*bold*, _italics_, [link](url) — single backslash NOT
needed for these). No emoji unless one genuinely earns a line.

Structure (include a section ONLY if data warrants it):
- One opening line: a short read of the day's state. Concrete, not 'it was a good day'.
- **Research / what's new.** Synthesize across sources. GROUP items by theme.
  Don't list every item. Surface 2-4 threads with inline [title](url) refs.
  If multiple items push the same idea, say so ('three takes on X converge on Y').
  Limit single-source dominance — if everything's tweets, name 1-2 best and move on.
- **Your work.** A short paragraph on what you shipped/touched. Numbers if they
  matter, not exhaustive lists.
- **Your body.** One paragraph. State the data + the implication ('sleep 61,
  readiness 72 — yesterday's pace is sustainable, today probably not').
- **Worth your time today.** One pick from the day's discoveries. Why it matters,
  not just the title.

Correlation intelligence: cross-domain patterns matter. Sleep dropping +
commits up = late nights. High calendar density + low steps = sedentary day.
Many bookmarks but quiet work = consumption phase. Call these out when present.

Skip sections cleanly when no data exists — never 'no data for this section'."""


def now_ms() -> int:
    return int(time.time() * 1000)


def fetch_discoveries(db, cutoff_ms: int) -> list[dict]:
    rows = db.execute(
        "SELECT title, url, summary, content, quality_score, source, captured_at "
        "FROM memory_items "
        "WHERE source LIKE 'discover:%' AND captured_at >= ? "
        "AND quality_score IS NOT NULL AND quality_score >= ? "
        "ORDER BY quality_score DESC, captured_at DESC LIMIT ?",
        (cutoff_ms, MIN_SCORE, MAX_DISCOVERIES),
    ).fetchall()
    return [
        {"title": t, "url": u, "summary": s or "", "content": (c or "")[:500],
         "score": q, "source": src.split(":", 1)[1] if ":" in src else src}
        for t, u, s, c, q, src, _ in rows
    ]


def fetch_github(db, cutoff_ms: int) -> list[dict]:
    rows = db.execute(
        "SELECT title, source, project_id, captured_at FROM memory_items "
        "WHERE source LIKE 'github:%' AND captured_at >= ? "
        "ORDER BY captured_at DESC LIMIT ?",
        (cutoff_ms, MAX_GITHUB),
    ).fetchall()
    return [
        {"title": t, "etype": src.split(":", 1)[1], "repo": p or "", "ts": ts}
        for t, src, p, ts in rows
    ]


def fetch_oura(db, cutoff_ms: int) -> list[dict]:
    # Pull last 2 days of health so we have yesterday at minimum
    rows = db.execute(
        "SELECT title, content, source, captured_at FROM memory_items "
        "WHERE source LIKE 'oura:%' AND captured_at >= ? "
        "ORDER BY captured_at DESC LIMIT 12",
        (cutoff_ms - 86400 * 1000,),
    ).fetchall()
    return [{"title": t, "content": c or "", "source": src.split(":", 1)[1], "ts": ts}
            for t, c, src, ts in rows]


def fetch_bookmarks(db, cutoff_ms: int) -> list[dict]:
    rows = db.execute(
        "SELECT title, url, captured_at FROM memory_items "
        "WHERE source = 'x-bookmark' AND captured_at >= ? "
        "ORDER BY captured_at DESC LIMIT ?",
        (cutoff_ms, MAX_BOOKMARKS),
    ).fetchall()
    return [{"title": t, "url": u} for t, u, _ in rows]


def build_prompt(discoveries, github, oura, bookmarks, lookback_h) -> str:
    parts = [f"Last {lookback_h} hours of the user's information landscape.\n"]

    if discoveries:
        parts.append("## Discoveries (scored vs interest profile)\n")
        for d in discoveries:
            parts.append(
                f"- [{d['score']:.2f} {d['source']}] {d['title']}\n"
                f"  url: {d['url']}\n"
                f"  why: {d['summary']}\n"
                f"  excerpt: {d['content'][:300]}\n"
            )
        parts.append("")

    if github:
        parts.append("## Work (GitHub events)\n")
        # Group by repo
        by_repo: dict[str, list[dict]] = {}
        for ev in github:
            by_repo.setdefault(ev["repo"] or "(unknown)", []).append(ev)
        for repo, evs in by_repo.items():
            parts.append(f"- {repo}: {len(evs)} event(s)")
            for ev in evs[:5]:
                parts.append(f"    {ev['etype']}: {ev['title']}")
        parts.append("")

    if oura:
        parts.append("## Body (Oura)\n")
        for o in oura:
            parts.append(f"- {o['title']}")
            parts.append(f"    {o['content']}")
        parts.append("")

    if bookmarks:
        parts.append("## Recent X bookmarks (what user just found compelling)\n")
        for b in bookmarks:
            parts.append(f"- {b['title']}")
        parts.append("")

    parts.append("Write the readout now. Markdown for Telegram. Be a synthesis, not a list.")
    return "\n".join(parts)


def main() -> int:
    db = get_db()
    try:
        cutoff_ms = now_ms() - LOOKBACK_HOURS * 3600 * 1000

        discoveries = fetch_discoveries(db, cutoff_ms)
        github = fetch_github(db, cutoff_ms)
        oura = fetch_oura(db, cutoff_ms)
        bookmarks = fetch_bookmarks(db, cutoff_ms)

        total = len(discoveries) + len(github) + len(oura) + len(bookmarks)
        log.info("discoveries=%d github=%d oura=%d bookmarks=%d (total=%d)",
                 len(discoveries), len(github), len(oura), len(bookmarks), total)

        if total == 0:
            log.info("nothing to digest")
            return 0  # silent

        prompt = build_prompt(discoveries, github, oura, bookmarks, LOOKBACK_HOURS)
        try:
            readout = llm_chat(prompt, system=SYSTEM, max_tokens=1800, temperature=0.5).strip()
        except Exception as e:
            log.error("synthesis failed: %s", e)
            return 1

        if readout:
            print(readout)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
