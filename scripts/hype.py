"""Every 4h: pull hype.replicate.dev (cross-platform ML/AI engagement ranking).

Aggregates trending items from GitHub (stars), HuggingFace (likes), Reddit
(upvotes), and Replicate. Filtered to past_day for incremental runs; dedup
on URL means a follow-up sees only new items.

Each item is HTML-scraped (no RSS/JSON on this site) then scored against the
interest profile, same pipeline as discover_rss / hf_papers. Source emoji
disambiguates the underlying platform; we tag captured items with both the
hype source and the underlying platform.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import sys

import httpx

from _common import (
    setup_logging, get_db, llm_json, get_profile_text,
)
from store import queries as q
from store.embed import embed_text

log = setup_logging("hype")

HYPE_URL = os.environ.get(
    "HYPE_URL",
    "https://hype.replicate.dev/?filter=past_day&sources=GitHub,HuggingFace,Reddit,Replicate",
)
SCORE_THRESHOLD = float(os.environ.get("HYPE_SCORE_THRESHOLD", "0.6"))
MAX_ITEMS = int(os.environ.get("HYPE_MAX_ITEMS", "60"))


# Each <li> on hype.replicate.dev:
#   <a href="URL" target="_blank" ...>TITLE</a>
#   <span class="...">EMOJI SCORE</span>
#   <p class="...">DESC (may be empty)</p>
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


def fetch_hype() -> str:
    resp = httpx.get(
        HYPE_URL, timeout=20.0,
        headers={"User-Agent": "hermes-aeon/0.1 (+https://github.com/vu1n/hermes-aeon)"},
    )
    resp.raise_for_status()
    return resp.text


def parse_items(page_html: str) -> list[dict]:
    items: list[dict] = []
    for url, title, emoji, count, desc in ITEM_RE.findall(page_html)[:MAX_ITEMS]:
        url = html.unescape(url)
        title = html.unescape(title).strip()
        desc = html.unescape(desc).strip()
        items.append({
            "url": url,
            "title": title,
            "source": SOURCE_BY_EMOJI.get(emoji, "other"),
            "engagement": int(count),
            "description": desc,
        })
    return items


def score_item(profile: str, item: dict) -> tuple[float, str]:
    prompt = (
        f"Interest profile:\n{profile}\n\n"
        f"Trending item:\n"
        f"  title: {item['title']}\n"
        f"  source: {item['source']} ({item['engagement']})\n"
        f"  description: {item['description'] or '(none)'}\n"
        f"  url: {item['url']}\n\n"
        "Score it now."
    )
    try:
        data = llm_json(prompt, system=SCORE_SYSTEM, max_tokens=200, temperature=0.1)
        return float(data.get("score", 0.0)), str(data.get("reason", ""))[:200]
    except Exception as e:
        log.debug("score failed for %s: %s", item["url"], e)
        return 0.0, f"score_error: {e}"


def main() -> int:
    db = get_db()
    try:
        profile = get_profile_text(db)
        if not profile:
            log.warning("no interest profile yet; skip")
            print("hype: no profile; skipped")
            return 0

        page = fetch_hype()
        items = parse_items(page)
        log.info("parsed %d items from hype", len(items))

        captured = 0
        skipped_dedup = 0
        for item in items:
            dedup = "hype:" + hashlib.sha256(item["url"].encode()).hexdigest()[:32]
            if db.execute("SELECT 1 FROM memory_items WHERE dedup_key = ? LIMIT 1",
                          (dedup,)).fetchone():
                skipped_dedup += 1
                continue

            score, reason = score_item(profile, item)
            if score < SCORE_THRESHOLD:
                continue

            content = (f"source: hype.replicate.dev ({item['source']})\n"
                       f"engagement: {item['engagement']}\n\n"
                       f"{item['description']}").strip()
            tags = ["discover", "hype", item["source"]]
            emb, model = embed_text(item["title"] + "\n" + item["description"],
                                    provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))
            q.capture_memory(
                db, type="link", domain="learning",
                title=item["title"],
                summary=reason,
                content=content,
                url=item["url"],
                tags=tags,
                quality_score=score,
                source=f"discover:hype-{item['source']}",
                dedup_key=dedup,
                embedding=emb, embedding_model=model,
            )
            captured += 1

        log.info("items=%d captured=%d skipped_dedup=%d threshold=%.2f",
                 len(items), captured, skipped_dedup, SCORE_THRESHOLD)
        if captured:
            print(f"hype: captured {captured} from {len(items)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
