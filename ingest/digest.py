"""Synthesize a daily readout across discoveries + work + body + bookmarks."""
from __future__ import annotations

import logging
import os
import time

from ._common import llm_chat

log = logging.getLogger("aeon.ingest.digest")


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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fetch_discoveries(db, cutoff_ms, min_score, limit):
    rows = db.execute(
        "SELECT title, url, summary, content, quality_score, source FROM memory_items "
        "WHERE source LIKE 'discover:%' AND captured_at >= ? "
        "AND quality_score IS NOT NULL AND quality_score >= ? "
        "ORDER BY quality_score DESC, captured_at DESC LIMIT ?",
        (cutoff_ms, min_score, limit),
    ).fetchall()
    return [{"title": t, "url": u, "summary": s or "", "content": (c or "")[:500],
             "score": q, "source": src.split(":", 1)[1] if ":" in src else src}
            for t, u, s, c, q, src in rows]


def _fetch_github(db, cutoff_ms, limit):
    rows = db.execute(
        "SELECT title, source, project_id FROM memory_items "
        "WHERE source LIKE 'github:%' AND captured_at >= ? "
        "ORDER BY captured_at DESC LIMIT ?",
        (cutoff_ms, limit),
    ).fetchall()
    return [{"title": t, "etype": src.split(":", 1)[1], "repo": p or ""}
            for t, src, p in rows]


def _fetch_oura(db, cutoff_ms):
    rows = db.execute(
        "SELECT title, content, source FROM memory_items "
        "WHERE source LIKE 'oura:%' AND captured_at >= ? "
        "ORDER BY captured_at DESC LIMIT 12",
        (cutoff_ms - 86400 * 1000,),
    ).fetchall()
    return [{"title": t, "content": c or "", "source": src.split(":", 1)[1]}
            for t, c, src in rows]


def _fetch_bookmarks(db, cutoff_ms, limit):
    rows = db.execute(
        "SELECT title, url FROM memory_items "
        "WHERE source = 'x-bookmark' AND captured_at >= ? "
        "ORDER BY captured_at DESC LIMIT ?",
        (cutoff_ms, limit),
    ).fetchall()
    return [{"title": t, "url": u} for t, u in rows]


def _build_prompt(discoveries, github, oura, bookmarks, hours) -> str:
    parts = [f"Last {hours} hours of the user's information landscape.\n"]
    if discoveries:
        parts.append("## Discoveries (scored vs interest profile)\n")
        for d in discoveries:
            parts.append(f"- [{d['score']:.2f} {d['source']}] {d['title']}\n"
                         f"  url: {d['url']}\n  why: {d['summary']}\n"
                         f"  excerpt: {d['content'][:300]}\n")
        parts.append("")
    if github:
        parts.append("## Work (GitHub events)\n")
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


def run(db, *, hours: int = 24) -> dict:
    cutoff_ms = _now_ms() - hours * 3600 * 1000
    min_score = float(os.environ.get("DIGEST_MIN_SCORE", "0.6"))
    max_discoveries = int(os.environ.get("DIGEST_MAX_DISCOVERIES", "20"))
    max_github = int(os.environ.get("DIGEST_MAX_GITHUB", "20"))
    max_bookmarks = int(os.environ.get("DIGEST_MAX_BOOKMARKS", "10"))

    discoveries = _fetch_discoveries(db, cutoff_ms, min_score, max_discoveries)
    github = _fetch_github(db, cutoff_ms, max_github)
    oura = _fetch_oura(db, cutoff_ms)
    bookmarks = _fetch_bookmarks(db, cutoff_ms, max_bookmarks)
    total = len(discoveries) + len(github) + len(oura) + len(bookmarks)

    log.info("discoveries=%d github=%d oura=%d bookmarks=%d (total=%d)",
             len(discoveries), len(github), len(oura), len(bookmarks), total)

    if total == 0:
        return {"text": "", "totals": {"discoveries": 0, "github": 0, "oura": 0, "bookmarks": 0}}

    prompt = _build_prompt(discoveries, github, oura, bookmarks, hours)
    text = llm_chat(prompt, system=SYSTEM, max_tokens=1800, temperature=0.5).strip()

    return {
        "text": text,
        "totals": {"discoveries": len(discoveries), "github": len(github),
                   "oura": len(oura), "bookmarks": len(bookmarks)},
        "window_hours": hours,
    }
