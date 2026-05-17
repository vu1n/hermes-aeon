---
name: aeon-memory
description: "Personal knowledge base — when and how to capture, search, and evolve memories across domains. Use when the user mentions notes, links, calendar events, health/nutrition logs, projects, contacts, or asks reflective questions."
platforms: [linux, macos, windows]
---

# Aeon Memory

The `hermes-aeon` provider is the user's personal knowledge base. Memories are typed, domain-scoped, and append-only by revision — every save preserves history.

## Domains (compartments)

Pick the most specific. When in doubt, `inbox` and triage later.

| Domain | What lives here |
|---|---|
| `inbox` | Unsorted captures awaiting triage; default for ambiguous URLs |
| `work` | Job/employer projects, work decisions, professional contacts |
| `side_projects` | Personal coding/creative projects (use `project_id` for the specific project) |
| `learning` | Articles, papers, courses, technical references being studied |
| `health` | Body, sleep, exercise, mental state, medical context |
| `life_admin` | Bills, taxes, appointments, household ops |
| `people_comms` | Conversations, contact details, relationship context |

## Types (shape)

| Type | Use for |
|---|---|
| `note` | Free-form text. Default when no other type fits. |
| `link` | URLs (the provider extracts content via the configured extractor). |
| `email` | Email content/threads pasted in. |
| `thread` | Multi-message conversation transcripts (Slack, SMS, etc.). |
| `task` | Things to do. Use sparingly — favor a real task system for active work. |
| `health_log` | Workouts, meals, sleep, vitals. Domain MUST be `health`. |
| `file` | File references (path or content snapshot). |
| `calendar_event` | Scheduled events. Set `event_start_ms` (and usually `event_end_ms`). |
| `contact` | Person details. Domain typically `people_comms` or `work`. |
| `other` | Last resort — flag for the user to retype or reclassify. |

## When to capture vs search vs recent

- **Capture** when the user says "save", "remember", "log", "note", "add", "I just …", or shares a URL. Pick a domain and type immediately — never ask.
- **Search** (`aeon_search`) before answering any factual question that depends on the user's history. Hybrid vector + FTS5 query — answers "what did I save about X" style questions.
- **Recent** (`aeon_recent`) for time-windowed "show me" queries — "what's new today", "top picks this week", "what HN came in". Paginated; pass `hours`, `min_score`, `source` to filter.
- **Both** when the user asks about something they've mentioned before — search first to ground, then capture any new information they share.

## When to use aeon_recent vs aeon_search

Reach for `aeon_recent` (NOT `aeon_search`) when the user asks for:
- "Show me today's discoveries" → `aeon_recent(hours=24)`
- "What's new this week" → `aeon_recent(hours=168)`
- "Top picks from HN today" → `aeon_recent(hours=24, source="discover:hn")`
- "Strong matches only" → add `min_score=0.7`
- "More" / "next page" → `aeon_recent(offset=<next_offset from previous response>)`

Sources for the `source` filter:
- `discover:` — all scored discoveries
- `discover:hn`, `discover:lobsters`, `discover:x`, `discover:hf-papers`, `discover:hype-*` — specific firehose
- `x-bookmark` — user's own bookmarks
- `github:` — work activity (commits, PRs, reviews)
- `oura:` — health data

## Pull / refresh tools (agent-triggerable fetchers)

Reach for these when the user asks to refresh a source explicitly, or when you've just answered a question and the underlying data is stale:

- `aeon_pull_bookmarks(backfill=False)` — pull new X bookmarks via xurl
- `aeon_pull_github()` — pull recent GitHub events (commits, PRs, reviews)
- `aeon_pull_oura(backfill=False, days?)` — pull Oura daily sleep/activity/readiness/workouts
- `aeon_pull_rss()` — pull HN + Lobste.rs, score vs profile, capture matches
- `aeon_pull_x()` — twitterapi.io topic search using profile-derived keywords
- `aeon_pull_hf_papers()` — Hugging Face daily papers, scored
- `aeon_pull_hype()` — hype.replicate.dev (GitHub/HF/Reddit/Replicate firehose)
- `aeon_derive_profile(lookback_days=90)` — re-derive interest profile from recent bookmarks
- `aeon_digest(hours=24)` — synthesize a narrative readout across all sources

All `aeon_pull_*` tools return `{captured, ...}` so you can summarize: "pulled 12 bookmarks, captured 12 new". `aeon_digest` returns `{text, totals, window_hours}` — the `text` is Telegram-markdown-ready.

Examples:
- *"refresh my bookmarks"* → `aeon_pull_bookmarks()`
- *"backfill the last 90 days of Oura"* → `aeon_pull_oura(backfill=True, days=90)`
- *"re-derive my profile"* → `aeon_derive_profile()`
- *"give me a digest of the last 3 days"* → `aeon_digest(hours=72)` then send the `.text` field
- *"pull HN now and tell me the top picks"* → `aeon_pull_rss()` then `aeon_recent(hours=1, source="discover:hn")`

## URL handling

User shares a URL → `aeon_capture` with `url=...`. The provider extracts content automatically. Don't paste the URL as plain content — it loses the body.

## Calendar

`aeon_calendar(start_ms, end_ms, domain?)` returns `calendar_event` rows. Use Unix milliseconds. For "today", anchor to the user's local midnight; for "this week", Mon–Sun.

## Memory evolution (revisions)

Memories are append-only. Use `aeon_update(memory_id, content)` when the user clarifies, corrects, or expands an existing memory — DO NOT capture a new one. The history shows how a concept evolved (e.g. "best-in-class X" can change over time).

## Auto-extract

`on_session_end` scans the transcript for URLs, decisions ("we decided to …"), and tasks ("I need to …") and captures them automatically. The agent doesn't need to call `aeon_capture` for ambient items it already discussed — but explicit captures are fine too (deduped by URL hash).

## TOM cards & capsules (read-only, surfaced via system prompt)

- **TOM cards** = volatile per-domain context with `expires_at`. Show up automatically in the system prompt when active.
- **Capsules** = period-bounded summaries (daily/weekly/monthly). Recent dailies appear in the system prompt.

You don't write these directly — they're produced by background workers. Just read what the system prompt surfaces.

## Tone implications

The aeon provider tracks a 5-axis tone state per session and surfaces it in context every turn (don't announce it, don't list the dials — blend it into voice). Useful nudges:

- `health` and `people_comms` domains → warmth↑, directness↓
- `work` and `side_projects` domains → directness↑, structure↑
- `learning` domain → evidence↑

Use `aeon_set_tone(preset=...)` when the user asks for a voice change ("be more direct", "coach me", "less analytical"). Presets: coach, analyst, creative, visionary, mentor. Or pass `axes` for a precise nudge.

## Privacy

If the user wants something forgotten, set `status=quarantined` rather than deleting (revisions stay; the row is excluded from active search). True deletion only on explicit "delete" / "purge" requests.

## Hard rules

- Never invent memories that don't exist in the store.
- Never paste a URL as `content` — use `url`.
- Never skip a domain/type pick — guess and continue, the user redirects.
- Don't announce tone state, presets, or axis values out loud.
