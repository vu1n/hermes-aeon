"""twitterapi.io topic search using keywords from interest profile."""
from __future__ import annotations

import logging
import os

import httpx

from ..store import queries as q
from ..store.embed import embed_text

from ._common import llm_json, get_profile_text

log = logging.getLogger("aeon.ingest.twitter")

TWITTERAPI_BASE = "https://api.twitterapi.io"


TOPIC_SYSTEM = """Extract X (Twitter) search topics from a person's interest profile.

Output strict JSON: {"topics": ["topic 1", "topic 2", ...]}
- 3–7 topics, each 2–5 words, suitable for X search (no special operators)
- Topics should reflect the SPECIFIC flavors in the profile, not generic categories"""


SCORE_SYSTEM = """You score how well a tweet matches a person's interests.

Output strict JSON: {"score": <float 0..1>, "reason": "<one short sentence>"}
- Tweets are short and lossy — judge the *signal* not the polish
- 0.5 = on a relevant topic but shallow take
- 0.7+ = substantive contribution to a topic the person cares about
- 1.0 = exactly the kind of high-signal tweet the person would bookmark"""


def _derive_topics(profile: str, max_topics: int) -> list[str]:
    try:
        data = llm_json(
            f"Interest profile:\n{profile}\n\nExtract X search topics now.",
            system=TOPIC_SYSTEM, max_tokens=400, temperature=0.2,
        )
        topics = data.get("topics") or []
        return [str(t).strip() for t in topics if str(t).strip()][:max_topics]
    except Exception as e:
        log.warning("topic derivation failed: %s", e)
        return []


def _search(query: str, limit: int, min_likes: int, min_retweets: int, key: str) -> list[dict]:
    resp = httpx.get(
        f"{TWITTERAPI_BASE}/twitter/tweet/advanced_search",
        headers={"x-api-key": key},
        params={
            "query": f"{query} min_faves:{min_likes} min_retweets:{min_retweets} -filter:replies",
            "queryType": "Top",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return (resp.json().get("tweets") or [])[:limit]


def _score(profile: str, tweet: dict) -> tuple[float, str]:
    text = tweet.get("text", "")
    author = (tweet.get("author") or {}).get("userName", "?")
    prompt = (f"Interest profile:\n{profile}\n\nTweet (@{author}):\n{text}\n\nScore it now.")
    try:
        data = llm_json(prompt, system=SCORE_SYSTEM, max_tokens=200, temperature=0.1)
        return float(data.get("score", 0.0)), str(data.get("reason", ""))[:200]
    except Exception as e:
        log.debug("score failed for tweet %s: %s", tweet.get("id"), e)
        return 0.0, f"score_error: {e}"


def run(db) -> dict:
    key = os.environ.get("TWITTER_API_IO_API_KEY")
    if not key:
        raise RuntimeError("TWITTER_API_IO_API_KEY not set")

    profile = get_profile_text(db)
    if not profile:
        return {"captured": 0, "seen": 0, "skipped_reason": "no_profile"}

    threshold = float(os.environ.get("DISCOVER_SCORE_THRESHOLD", "0.7"))
    max_topics = int(os.environ.get("DISCOVER_X_MAX_TOPICS", "5"))
    max_per_topic = int(os.environ.get("DISCOVER_X_MAX_PER_TOPIC", "20"))
    min_likes = int(os.environ.get("DISCOVER_X_MIN_LIKES", "50"))
    min_retweets = int(os.environ.get("DISCOVER_X_MIN_RETWEETS", "10"))

    topics = _derive_topics(profile, max_topics)
    if not topics:
        return {"captured": 0, "seen": 0, "topics": [], "skipped_reason": "no_topics"}

    total_seen = total_captured = 0
    for topic in topics:
        try:
            tweets = _search(topic, max_per_topic, min_likes, min_retweets, key)
        except Exception as e:
            log.warning("twitterapi search '%s' failed: %s", topic, e)
            continue

        for tweet in tweets:
            total_seen += 1
            tid = tweet.get("id")
            if not tid:
                continue
            dedup = f"x-discover:{tid}"
            if db.execute("SELECT 1 FROM memory_items WHERE dedup_key = ? LIMIT 1",
                          (dedup,)).fetchone():
                continue
            score, reason = _score(profile, tweet)
            if score < threshold:
                continue

            text = tweet.get("text", "")
            author = (tweet.get("author") or {}).get("userName", "")
            title = f"@{author}: {text[:120]}".replace("\n", " ").strip()
            url = f"https://x.com/i/web/status/{tid}"
            emb, model = embed_text(text or title,
                                    provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))
            q.capture_memory(
                db, type="link", domain="learning",
                title=title, summary=reason, content=text, url=url,
                tags=["discover", "x", topic],
                quality_score=score,
                source="discover:x",
                dedup_key=dedup,
                embedding=emb, embedding_model=model,
            )
            total_captured += 1

    log.info("topics=%d seen=%d captured=%d threshold=%.2f",
             len(topics), total_seen, total_captured, threshold)
    return {"topics": topics, "seen": total_seen, "captured": total_captured,
            "threshold": threshold}
