"""Every 4h: search X via twitterapi.io using keywords derived from the interest profile.

twitterapi.io is the cheap firehose ($0.15/1k tweets vs X official $5/1k for general reads).
Used only for PUBLIC topic discovery; bookmarks go through the official xurl path.

Topic keywords are extracted from the current interest profile via the LLM each run.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import httpx

from _common import (
    setup_logging, get_db, llm_json, get_profile_text,
)
from store import queries as q
from store.embed import embed_text

log = setup_logging("discover_x")

TWITTERAPI_BASE = "https://api.twitterapi.io"
TWITTERAPI_KEY = os.environ.get("TWITTER_API_IO_API_KEY")
SCORE_THRESHOLD = float(os.environ.get("DISCOVER_SCORE_THRESHOLD", "0.7"))  # higher bar for noisy firehose
MAX_TOPICS = int(os.environ.get("DISCOVER_X_MAX_TOPICS", "5"))
MAX_PER_TOPIC = int(os.environ.get("DISCOVER_X_MAX_PER_TOPIC", "20"))
MIN_LIKES = int(os.environ.get("DISCOVER_X_MIN_LIKES", "50"))
MIN_RETWEETS = int(os.environ.get("DISCOVER_X_MIN_RETWEETS", "10"))


TOPIC_SYSTEM = """Extract X (Twitter) search topics from a person's interest profile.

Output strict JSON: {"topics": ["topic 1", "topic 2", ...]}
- 3–7 topics, each 2–5 words, suitable for X search (no special operators)
- Topics should reflect the SPECIFIC flavors in the profile, not generic categories
- Examples of good topics: "agent harness engineering", "ZK proofs production"
- Examples of bad topics: "AI", "tech", "programming" (too generic)"""


SCORE_SYSTEM = """You score how well a tweet matches a person's interests.

Output strict JSON: {"score": <float 0..1>, "reason": "<one short sentence>"}
- Tweets are short and lossy — judge the *signal* not the polish
- 0.5 = on a relevant topic but shallow take
- 0.7+ = substantive contribution to a topic the person cares about
- 1.0 = exactly the kind of high-signal tweet the person would bookmark"""


def derive_topics(profile: str) -> list[str]:
    try:
        data = llm_json(
            f"Interest profile:\n{profile}\n\nExtract X search topics now.",
            system=TOPIC_SYSTEM, max_tokens=400, temperature=0.2,
        )
        topics = data.get("topics") or []
        return [str(t).strip() for t in topics if str(t).strip()][:MAX_TOPICS]
    except Exception as e:
        log.warning("topic derivation failed: %s", e)
        return []


def search_twitterapi(query: str, limit: int) -> list[dict]:
    """Search via twitterapi.io advanced search endpoint."""
    if not TWITTERAPI_KEY:
        raise RuntimeError("TWITTER_API_IO_API_KEY not set")
    resp = httpx.get(
        f"{TWITTERAPI_BASE}/twitter/tweet/advanced_search",
        headers={"x-api-key": TWITTERAPI_KEY},
        params={
            "query": f"{query} min_faves:{MIN_LIKES} min_retweets:{MIN_RETWEETS} -filter:replies",
            "queryType": "Top",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    tweets = resp.json().get("tweets") or []
    return tweets[:limit]


def score_tweet(profile: str, tweet: dict) -> tuple[float, str]:
    text = tweet.get("text", "")
    author = (tweet.get("author") or {}).get("userName", "?")
    prompt = (
        f"Interest profile:\n{profile}\n\n"
        f"Tweet (@{author}):\n{text}\n\n"
        "Score it now."
    )
    try:
        data = llm_json(prompt, system=SCORE_SYSTEM, max_tokens=200, temperature=0.1)
        return float(data.get("score", 0.0)), str(data.get("reason", ""))[:200]
    except Exception as e:
        log.debug("score failed for tweet %s: %s", tweet.get("id"), e)
        return 0.0, f"score_error: {e}"


def main() -> int:
    if not TWITTERAPI_KEY:
        log.error("TWITTER_API_IO_API_KEY not set; cannot run X discovery")
        return 2

    db = get_db()
    try:
        profile = get_profile_text(db)
        if not profile:
            log.warning("no interest profile yet; skip")
            print("discover_x: no profile; skipped")
            return 0

        topics = derive_topics(profile)
        if not topics:
            print("discover_x: no topics derived; skipped")
            return 0

        log.info("topics: %s", topics)
        total_seen = 0
        total_captured = 0

        for topic in topics:
            try:
                tweets = search_twitterapi(topic, MAX_PER_TOPIC)
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

                score, reason = score_tweet(profile, tweet)
                if score < SCORE_THRESHOLD:
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
                 len(topics), total_seen, total_captured, SCORE_THRESHOLD)
        if total_captured:
            print(f"discover_x: captured {total_captured} from {total_seen}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
