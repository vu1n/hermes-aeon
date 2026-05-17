"""Pull HN + Lobste.rs RSS, score vs profile, capture matches. Returns dict."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import xml.etree.ElementTree as ET

import httpx

from ..store import queries as q
from ..store.embed import embed_text

from ._common import llm_json, get_profile_text

log = logging.getLogger("aeon.ingest.rss")

DEFAULT_FEEDS = [
    {"name": "hn-frontpage",     "url": "https://hnrss.org/frontpage",     "source": "hn"},
    {"name": "lobsters-hottest", "url": "https://lobste.rs/rss",           "source": "lobsters"},
]


def _load_feeds() -> list[dict]:
    raw = os.environ.get("HERMES_AEON_RSS_FEEDS")
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            log.warning("HERMES_AEON_RSS_FEEDS parse failed: %s; using defaults", e)
    return DEFAULT_FEEDS


def _fetch_rss(url: str) -> list[dict]:
    resp = httpx.get(url, timeout=20.0, follow_redirects=True,
                     headers={"User-Agent": "hermes-aeon/0.1"})
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    items: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        if title and link:
            items.append({"title": title, "link": link, "description": desc, "guid": guid})

    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = (entry.findtext("a:title", namespaces=ns) or "").strip()
        link_el = entry.find("a:link", namespaces=ns)
        link = (link_el.get("href") if link_el is not None else "").strip()
        desc = (entry.findtext("a:summary", namespaces=ns) or "").strip()
        guid = (entry.findtext("a:id", namespaces=ns) or link or title).strip()
        if title and link:
            items.append({"title": title, "link": link, "description": desc, "guid": guid})

    return items


SCORE_SYSTEM = """You score how well an article matches a person's interests.

Output strict JSON: {"score": <float 0..1>, "reason": "<one short sentence>"}
- 0.0 = no relevance
- 0.5 = tangentially related
- 0.8+ = strong match — would be high-signal for this person
- 1.0 = perfect bullseye

Be calibrated: most articles in a generic firehose should score under 0.5.
Only the genuine matches against the profile's specific themes should clear 0.7."""


def _score(profile: str, item: dict) -> tuple[float, str]:
    prompt = (
        f"Interest profile:\n{profile}\n\n"
        f"Article:\n  title: {item['title']}\n  url: {item['link']}\n"
        f"  excerpt: {item['description'][:500]}\n\nScore it now."
    )
    try:
        data = llm_json(prompt, system=SCORE_SYSTEM, max_tokens=200, temperature=0.1)
        return float(data.get("score", 0.0)), str(data.get("reason", ""))[:200]
    except Exception as e:
        log.debug("score failed for %s: %s", item["link"], e)
        return 0.0, f"score_error: {e}"


def run(db) -> dict:
    profile = get_profile_text(db)
    if not profile:
        log.warning("no interest profile yet")
        return {"captured": 0, "scored": 0, "seen": 0, "skipped_reason": "no_profile"}

    threshold = float(os.environ.get("DISCOVER_SCORE_THRESHOLD", "0.6"))
    max_per_feed = int(os.environ.get("DISCOVER_MAX_PER_FEED", "30"))
    feeds = _load_feeds()
    total_seen = total_scored = total_captured = 0

    for feed in feeds:
        try:
            items = _fetch_rss(feed["url"])[:max_per_feed]
        except Exception as e:
            log.warning("feed %s fetch failed: %s", feed["name"], e)
            continue

        for item in items:
            total_seen += 1
            dedup = "rss:" + hashlib.sha256(item["guid"].encode()).hexdigest()[:32]
            if db.execute("SELECT 1 FROM memory_items WHERE dedup_key = ? LIMIT 1",
                          (dedup,)).fetchone():
                continue
            score, reason = _score(profile, item)
            total_scored += 1
            if score < threshold:
                continue

            content = item["description"] or item["title"]
            emb, model = embed_text(item["title"] + "\n" + content,
                                    provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))
            q.capture_memory(
                db, type="link", domain="learning",
                title=item["title"], summary=reason, content=content,
                url=item["link"],
                tags=["discover", feed["source"]],
                quality_score=score,
                source=f"discover:{feed['source']}",
                dedup_key=dedup,
                embedding=emb, embedding_model=model,
            )
            total_captured += 1

    log.info("feeds=%d seen=%d scored=%d captured=%d threshold=%.2f",
             len(feeds), total_seen, total_scored, total_captured, threshold)
    return {"feeds": len(feeds), "seen": total_seen, "scored": total_scored,
            "captured": total_captured, "threshold": threshold}
