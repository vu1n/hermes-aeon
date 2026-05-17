"""hype.replicate.dev cross-platform ML/AI engagement firehose."""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re

import httpx

from ..store import queries as q
from ..store.embed import embed_text

from ._common import llm_json, get_profile_text

log = logging.getLogger("aeon.ingest.hype")

ITEM_RE = re.compile(
    r'<a\s+href="([^"]+)"\s+target="_blank"[^>]*>([^<]+)</a>'
    r'\s*<span[^>]*>([⭐👽🤗®])\s*(\d+)</span>'
    r'\s*</div>\s*<p[^>]*>([^<]*)</p>',
    re.DOTALL,
)

SOURCE_BY_EMOJI = {"⭐": "github", "👽": "reddit", "🤗": "huggingface", "®": "replicate"}

SCORE_SYSTEM = """You score how well a trending AI/ML item matches a person's interests.

Output strict JSON: {"score": <float 0..1>, "reason": "<one short sentence>"}
- Items range from serious research / tools (high signal) to noise (e.g., a 'crop recommendation system' or a 'resume practice' repo at the top of the day's list)
- 0.4–0.6 = on-topic but not for this person specifically
- 0.7+ = something they would actually want to know about
- 1.0 = exactly the kind of trending thing they'd bookmark

Discount low-information items (single-purpose tutorials, university coursework repos, generic LLM wrappers). Reward novel research, infra, agents, tools, frameworks, and substantive discussion."""


def _parse_items(page_html: str, max_items: int) -> list[dict]:
    items: list[dict] = []
    for url, title, emoji, count, desc in ITEM_RE.findall(page_html)[:max_items]:
        items.append({
            "url": html.unescape(url),
            "title": html.unescape(title).strip(),
            "source": SOURCE_BY_EMOJI.get(emoji, "other"),
            "engagement": int(count),
            "description": html.unescape(desc).strip(),
        })
    return items


def _score(profile: str, item: dict) -> tuple[float, str]:
    prompt = (
        f"Interest profile:\n{profile}\n\n"
        f"Trending item:\n  title: {item['title']}\n"
        f"  source: {item['source']} ({item['engagement']})\n"
        f"  description: {item['description'] or '(none)'}\n"
        f"  url: {item['url']}\n\nScore it now."
    )
    try:
        data = llm_json(prompt, system=SCORE_SYSTEM, max_tokens=200, temperature=0.1)
        return float(data.get("score", 0.0)), str(data.get("reason", ""))[:200]
    except Exception as e:
        log.debug("score failed for %s: %s", item["url"], e)
        return 0.0, f"score_error: {e}"


def run(db) -> dict:
    profile = get_profile_text(db)
    if not profile:
        return {"captured": 0, "seen": 0, "skipped_reason": "no_profile"}

    url = os.environ.get(
        "HYPE_URL",
        "https://hype.replicate.dev/?filter=past_day&sources=GitHub,HuggingFace,Reddit,Replicate",
    )
    threshold = float(os.environ.get("HYPE_SCORE_THRESHOLD", "0.6"))
    max_items = int(os.environ.get("HYPE_MAX_ITEMS", "60"))

    resp = httpx.get(url, timeout=20.0,
                     headers={"User-Agent": "hermes-aeon/0.1"})
    resp.raise_for_status()
    items = _parse_items(resp.text, max_items)
    log.info("parsed %d items from hype", len(items))

    captured = skipped_dedup = 0
    for item in items:
        dedup = "hype:" + hashlib.sha256(item["url"].encode()).hexdigest()[:32]
        if db.execute("SELECT 1 FROM memory_items WHERE dedup_key = ? LIMIT 1",
                      (dedup,)).fetchone():
            skipped_dedup += 1
            continue

        score, reason = _score(profile, item)
        if score < threshold:
            continue

        content = (f"source: hype.replicate.dev ({item['source']})\n"
                   f"engagement: {item['engagement']}\n\n{item['description']}").strip()
        emb, model = embed_text(item["title"] + "\n" + item["description"],
                                provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))
        q.capture_memory(
            db, type="link", domain="learning",
            title=item["title"], summary=reason, content=content, url=item["url"],
            tags=["discover", "hype", item["source"]],
            quality_score=score,
            source=f"discover:hype-{item['source']}",
            dedup_key=dedup,
            embedding=emb, embedding_model=model,
        )
        captured += 1

    log.info("items=%d captured=%d skipped_dedup=%d threshold=%.2f",
             len(items), captured, skipped_dedup, threshold)
    return {"seen": len(items), "captured": captured,
            "skipped_dedup": skipped_dedup, "threshold": threshold}
