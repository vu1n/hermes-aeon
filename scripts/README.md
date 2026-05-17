# hermes-aeon cron scripts

Pure-script background workers wired via `hermes cron create --script ... --no-agent`.
They share state with the running hermes-aeon plugin (same `~/.hermes/aeon.db`).

## Flow

```
   X bookmarks (xurl, $0.001/item)
   ──────► bookmarks_fetch.py [hourly]
   ──────►   memory_items (source=x-bookmark)
              │
              ▼
   derive_profile.py [daily]
   ──────►   memory_items (dedup_key=profile:interests)
              │
              ├──────► discover_rss.py [hourly]    HN + Lobste.rs RSS
              │           ──► score vs profile ──► capture top picks (quality_score)
              │
              └──────► discover_x.py [4h]          twitterapi.io topic search
                          ──► score vs profile ──► capture top picks (quality_score)

   digest.py [daily] ──► top scored items from last 24h ──► stdout ──► Telegram
```

## Cost ceiling (typical personal use)

- X bookmarks: ~$0.10/month (new bookmarks via X official API @ $0.001)
- twitterapi.io discovery: ~$0.50–$3/month (depending on cadence + topic count)
- LLM scoring: $0 (Groq free tier, ~thousands of calls/day)
- Embeddings: $0 (Gemini free tier)
- HN/Lobste.rs: $0 (public RSS)

## Configuration (env vars)

| Var | Default | What |
|---|---|---|
| `X_USER_ID` | `15781023` | X numeric user ID for bookmarks fetch |
| `XURL_APP` | `hermes-aeon` | xurl app profile name |
| `CRON_LLM_PROVIDER` | `groq` | `groq` \| `openrouter` \| `gemini` |
| `CRON_LLM_MODEL` | (provider default) | Override the model |
| `HERMES_AEON_EMBED` | `gemini` | Embedding provider |
| `BOOKMARKS_MAX_RESULTS` | `100` | Per-page bookmark fetch size |
| `PROFILE_LOOKBACK_DAYS` | `90` | How far back to read bookmarks for profile |
| `PROFILE_MIN_BOOKMARKS` | `5` | Skip profile derivation if fewer bookmarks |
| `DISCOVER_SCORE_THRESHOLD` | `0.6` (RSS), `0.7` (X) | Capture if score ≥ this |
| `DISCOVER_MAX_PER_FEED` | `30` | RSS items per feed per run |
| `DISCOVER_X_MAX_TOPICS` | `5` | X topics extracted per run |
| `DISCOVER_X_MAX_PER_TOPIC` | `20` | Tweets per topic |
| `DISCOVER_X_MIN_LIKES` | `50` | twitterapi.io engagement filter |
| `DISCOVER_X_MIN_RETWEETS` | `10` | twitterapi.io engagement filter |
| `DIGEST_LOOKBACK_HOURS` | `24` | Digest window |
| `DIGEST_TOP_N` | `8` | Max items per digest |
| `DIGEST_MIN_SCORE` | `0.65` | Digest filter |
| `HERMES_AEON_RSS_FEEDS` | (defaults to HN + Lobste.rs) | JSON array of `{name, url, source}` |

## Manual runs

```sh
# from the VPS
cd /opt/hermes-aeon
/usr/local/lib/hermes-agent/venv/bin/python scripts/bookmarks_fetch.py
/usr/local/lib/hermes-agent/venv/bin/python scripts/bookmarks_fetch.py --backfill   # paginated all-history pull
/usr/local/lib/hermes-agent/venv/bin/python scripts/derive_profile.py
/usr/local/lib/hermes-agent/venv/bin/python scripts/discover_rss.py
/usr/local/lib/hermes-agent/venv/bin/python scripts/discover_x.py
/usr/local/lib/hermes-agent/venv/bin/python scripts/digest.py
```
