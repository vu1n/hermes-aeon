"""Hugging Face daily papers, scored vs profile."""
from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

from store import queries as q
from store.embed import embed_text

from ._common import llm_json, get_profile_text

log = logging.getLogger("aeon.ingest.hf_papers")

HF_API = "https://huggingface.co/api/daily_papers"

SCORE_SYSTEM = """You score how well a research paper matches a person's interests.

Output strict JSON: {"score": <float 0..1>, "reason": "<one short sentence>"}
- 0.0 = no relevance
- 0.5 = adjacent topic, the person might glance at it
- 0.7 = clear match, worth their time
- 0.9+ = exactly the kind of paper they'd bookmark and read deeply

Be calibrated. HF daily papers are already curated; most of them are good
research but only some align with this person's specific themes."""


def _score(profile: str, paper: dict) -> tuple[float, str]:
    title = paper.get("title") or ""
    summary = paper.get("summary") or ""
    upvotes = paper.get("upvotes", 0)
    prompt = (
        f"Interest profile:\n{profile}\n\n"
        f"Paper:\n  title: {title}\n  upvotes: {upvotes}\n  abstract: {summary[:1500]}\n\n"
        "Score it now."
    )
    try:
        data = llm_json(prompt, system=SCORE_SYSTEM, max_tokens=200, temperature=0.1)
        return float(data.get("score", 0.0)), str(data.get("reason", ""))[:200]
    except Exception as e:
        log.debug("score failed for %s: %s", paper.get("id"), e)
        return 0.0, f"score_error: {e}"


def _parse_ts(iso: str) -> int:
    s = iso.replace("Z", "+00:00") if iso else ""
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return int(datetime.utcnow().timestamp() * 1000)


def run(db) -> dict:
    profile = get_profile_text(db)
    if not profile:
        return {"captured": 0, "seen": 0, "skipped_reason": "no_profile"}

    threshold = float(os.environ.get("HF_SCORE_THRESHOLD", "0.55"))
    max_papers = int(os.environ.get("HF_MAX_PAPERS", "30"))

    resp = httpx.get(HF_API, timeout=20.0,
                     headers={"User-Agent": "hermes-aeon/0.1"})
    resp.raise_for_status()
    raw_items = resp.json()[:max_papers]

    captured = 0
    for item in raw_items:
        paper = item.get("paper") or {}
        pid = paper.get("id")
        if not pid:
            continue
        dedup = f"hf-paper:{pid}"
        if db.execute("SELECT 1 FROM memory_items WHERE dedup_key = ? LIMIT 1",
                      (dedup,)).fetchone():
            continue

        score, reason = _score(profile, paper)
        if score < threshold:
            continue

        title = paper.get("title") or pid
        summary = paper.get("summary") or ""
        authors = ", ".join((a.get("name") or "") for a in (paper.get("authors") or [])[:5])
        upvotes = paper.get("upvotes", 0)
        url = f"https://huggingface.co/papers/{pid}"
        github = paper.get("githubRepo") or ""
        project = paper.get("projectPage") or ""

        content_parts = [f"authors: {authors}", f"upvotes: {upvotes}",
                         f"arxiv: https://arxiv.org/abs/{pid}"]
        if github:
            content_parts.append(f"github: {github}")
        if project:
            content_parts.append(f"project: {project}")
        content_parts.append("")
        content_parts.append(summary)
        content = "\n".join(content_parts)

        tags = ["hf-paper", f"arxiv:{pid}"]
        if github:
            tags.append("has-code")

        ts_ms = _parse_ts(paper.get("submittedOnDailyAt") or paper.get("publishedAt") or "")
        emb, model = embed_text(title + "\n" + summary,
                                provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))
        q.capture_memory(
            db, type="link", domain="learning",
            title=title, summary=reason, content=content, url=url,
            tags=tags, quality_score=score,
            source="discover:hf-papers",
            dedup_key=dedup, captured_at=ts_ms,
            embedding=emb, embedding_model=model,
        )
        captured += 1

    log.info("seen=%d captured=%d threshold=%.2f", len(raw_items), captured, threshold)
    return {"seen": len(raw_items), "captured": captured, "threshold": threshold}
