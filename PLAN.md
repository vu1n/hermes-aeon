# hermes-aeon Foundation Plan

Standalone hermes-agent plugin that ports aeon's soul + tone codec + memory model onto hermes. No hermes fork, no data migration from aeon v1/v2 (concepts only). Installed to `~/.hermes/plugins/hermes-aeon/`.

## Architecture

- **One plugin, three capabilities**: MemoryProvider (burns hermes's single provider slot), tone codec (pre_llm_call hook), aeon-memory skill.
- **Storage**: libSQL embedded with optional Turso replica sync. Native vector (`F32_BLOB` + `vector_distance_cos`) + FTS5 hybrid.
- **Soul/tone split**: `~/.hermes/SOUL.md` holds invariant identity; tone codec injects a per-message block, never announces tone.
- **Web extraction**: r.jina.ai provider added alongside existing `tools/browser_providers/firecrawl.py`. Capture-note routes URL captures through it.

## Repo layout (`~/code/hermes-aeon`)

```
hermes-aeon/
  plugin.yaml                          # name: hermes-aeon, hooks: [on_session_end, pre_llm_call]
  pyproject.toml                       # deps: libsql-experimental, pytest
  __init__.py                          # register(ctx) — registers provider, tools, skill, tone hook
  README.md
  PLAN.md                              # this file once bootstrapped
  store/
    __init__.py
    schema.sql                         # tables below
    db.py                              # libSQL connection (local + optional Turso sync)
    queries.py                         # typed query layer (Vu's "JSON at boundary" pattern)
    artifacts.py                       # placeholder; only text-in-row for v1
  provider.py                          # AeonMemoryProvider(MemoryProvider)
  tone/
    __init__.py
    codec.py                           # 5-axis state, presets, blending math
    hook.py                            # pre_llm_call injector
    state.py                           # per-session tone state (session_id → ToneState)
  tools/
    __init__.py
    search_aeon.py
    capture_note.py
    get_calendar.py
    set_tone.py                        # runtime tone adjustment
    get_tone.py
    browser_providers/
      jina.py                          # r.jina.ai extractor
  skills/
    aeon-memory/
      SKILL.md                         # judgment: when/what/which-domain
  tests/
    test_schema.py
    test_provider.py
    test_tone_codec.py
    test_tools.py

~/.hermes/SOUL.md                      # base identity (created separately, not in repo)
```

## Schema (`store/schema.sql`)

```sql
-- Source of truth: typed records
CREATE TABLE memory_items (
  id              TEXT PRIMARY KEY,           -- uuid7
  user_id         TEXT NOT NULL DEFAULT 'local',
  type            TEXT NOT NULL,              -- note|link|email|thread|task|health_log|file|calendar_event|contact|other
  domain          TEXT NOT NULL,              -- inbox|work|side_projects|learning|health|life_admin|people_comms
  status          TEXT NOT NULL DEFAULT 'active', -- active|archived|quarantined
  title           TEXT,
  summary         TEXT,
  content         TEXT,                       -- extracted text body (markdown)
  url             TEXT,
  entities        TEXT,                       -- JSON dict
  tags            TEXT,                       -- JSON array
  summary_bullets TEXT,                       -- JSON array
  project_id      TEXT,
  quality_score   REAL,
  source          TEXT,                       -- jina|firecrawl|manual|session-extract|...
  captured_at     INTEGER NOT NULL,           -- unix ms
  event_start     INTEGER,                    -- for calendar_event
  event_end       INTEGER,
  accessed_at     INTEGER,
  access_count    INTEGER DEFAULT 0,
  dedup_key       TEXT,
  artifact_sha    TEXT,                       -- reserved for v2 binary artifacts
  current_revision INTEGER DEFAULT 1
);
CREATE INDEX idx_memory_domain_type ON memory_items(domain, type, status);
CREATE INDEX idx_memory_project ON memory_items(project_id) WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX idx_memory_dedup ON memory_items(dedup_key) WHERE dedup_key IS NOT NULL;

-- Append-only history for concept evolution
CREATE TABLE memory_revisions (
  memory_id   TEXT NOT NULL REFERENCES memory_items(id),
  revision_n  INTEGER NOT NULL,
  content     TEXT,                           -- snapshot
  summary     TEXT,
  source      TEXT,
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (memory_id, revision_n)
);

-- Native libSQL vectors (no extension)
CREATE TABLE memory_embeddings (
  memory_id   TEXT PRIMARY KEY REFERENCES memory_items(id),
  embedding   F32_BLOB(768) NOT NULL,         -- gemini text-embedding-004
  model       TEXT NOT NULL,
  embedded_at INTEGER NOT NULL
);
-- Note: libSQL vector search is linear scan; fine for personal scale (<100k items).
-- Add ANN index later if needed.

-- Volatile/expiring TOM cards (per-domain rolling context)
CREATE TABLE tom_cards (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL DEFAULT 'local',
  domain      TEXT NOT NULL,
  content     TEXT NOT NULL,
  expires_at  INTEGER NOT NULL,
  created_at  INTEGER NOT NULL
);
CREATE INDEX idx_tom_domain_expires ON tom_cards(domain, expires_at);

-- Period-bounded summaries
CREATE TABLE capsules (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL DEFAULT 'local',
  domain      TEXT NOT NULL,
  period      TEXT NOT NULL,                  -- daily|weekly|monthly
  period_start INTEGER NOT NULL,
  period_end  INTEGER NOT NULL,
  summary     TEXT NOT NULL,
  created_at  INTEGER NOT NULL
);
CREATE INDEX idx_capsules_domain_period ON capsules(domain, period, period_start);

-- Hybrid lexical search
CREATE VIRTUAL TABLE memory_fts USING fts5(
  memory_id UNINDEXED,
  title,
  summary,
  content,
  tokenize = 'porter unicode61'
);
```

**Hybrid retrieval query shape**:
```sql
WITH vec AS (
  SELECT memory_id, vector_distance_cos(embedding, vector32(?)) AS dist
  FROM memory_embeddings ORDER BY dist LIMIT 50
),
fts AS (
  SELECT memory_id, bm25(memory_fts) AS rank
  FROM memory_fts WHERE memory_fts MATCH ? LIMIT 50
)
SELECT m.*,
  COALESCE(1.0/(60+v.dist),0) + COALESCE(1.0/(60-f.rank),0) AS rrf_score
FROM memory_items m
LEFT JOIN vec v ON v.memory_id = m.id
LEFT JOIN fts f ON f.memory_id = m.id
WHERE m.status = 'active'
  AND (? IS NULL OR m.domain = ?)
  AND (? IS NULL OR m.type = ?)
ORDER BY rrf_score DESC LIMIT ?;
```

## Provider lifecycle (`provider.py`)

```python
class AeonMemoryProvider(MemoryProvider):
    name = "aeon"

    def initialize(self, ctx): ...
        # Open libSQL connection. Run schema.sql if first boot.

    def system_prompt_block(self) -> str:
        # Return active (non-expired) TOM cards + today's capsules per active domain.
        # Wrap in <memory-context>...</memory-context> (hermes sanitizes outbound).

    def prefetch(self, user_message: str) -> str:
        # Phase A (<800ms target): TOM cards + active-domain capsules
        # Phase B (<3s target): hybrid search (vector + FTS5 + RRF), top-k by domain hint
        # Return merged context block

    def sync_turn(self, user_msg: str, assistant_response: str):
        # Cheap post-turn: increment access_count for memories retrieved this turn.
        # Heavy capture deferred to on_session_end.

    def on_session_end(self, transcript):
        # Auto-extract: links shared (→ link), decisions made (→ note in active project),
        # health entries mentioned (→ health_log), people referenced (→ contact upsert).
        # LLM call to extract, then write via store/queries.py.

    def shutdown(self): ...
```

## Tone codec spec (`tone/codec.py`)

```python
AXES = ("warmth", "directness", "structure", "evidence", "playfulness")

DEFAULT_TONE = ToneState(warmth=0.6, directness=0.6, structure=0.6, evidence=0.5, playfulness=0.3)

PRESETS = {
  "coach":     ToneState(0.8, 0.6, 0.7, 0.5, 0.3),
  "analyst":   ToneState(0.4, 0.8, 0.8, 0.8, 0.2),
  "creative":  ToneState(0.6, 0.5, 0.5, 0.5, 0.5),
  "visionary": ToneState(0.4, 0.8, 0.7, 0.6, 0.3),
  "mentor":    ToneState(0.7, 0.5, 0.5, 0.5, 0.5),
}

def blend(current: ToneState, preset: ToneState, weight: float) -> ToneState: ...

def derive_from_message(msg: str, current: ToneState) -> ToneState:
    # Intent patterns ("coach me" → coach @ 0.7), domain hints
    # (health/people → warmth↑), emotional state (stressed → warmth↑/directness↓).

def tone_to_block(state: ToneState) -> str:
    # Returns prose block, never names a preset. e.g.:
    # "Tone state (blend naturally; do not announce):\n  warmth: 0.8 (supportive, encouraging)\n  ..."
```

**Hook** (`tone/hook.py`): registered as `pre_llm_call`. Reads session ID from ctx, derives or recalls tone state from `tone/state.py` (in-memory dict keyed by session_id, persisted to `~/.hermes/hermes-aeon/tone.json` per-session), prepends `tone_to_block(state)` to the system prompt slot.

## Tools

| Tool | Args | Returns |
|---|---|---|
| `search_aeon` | query: str, domain?: str, type?: str, project_id?: str, limit?: int=10 | JSON array of HydratedMemoryItem |
| `capture_note` | content?: str, url?: str, domain: str, type: str, title?: str, tags?: str[], project_id?: str | { id, status } |
| `get_calendar` | start: int, end: int, domain?: str | JSON array of calendar_event items |
| `set_tone` | preset?: str, axes?: {warmth?, directness?, structure?, evidence?, playfulness?}, weight?: float=0.7 | new ToneState |
| `get_tone` | — | current ToneState |

`capture_note` with `url` and no `content` invokes the jina provider, extracts text, writes content + summary (LLM call), embeds, dedup-checks by URL hash.

## r.jina.ai provider (`tools/browser_providers/jina.py`)

```python
def fetch(url: str) -> str:
    return httpx.get(f"https://r.jina.ai/{url}", timeout=30).text
```

Selectable via config: `plugins.hermes-aeon.extractor: jina | firecrawl`. Default `jina` (no API key).

## SKILL.md outline (`skills/aeon-memory/SKILL.md`)

Frontmatter: name, triggers (KB-relevant terms), description.

Body covers:
- The 7 domains + 10 types — when each applies (one-liner per cell, not 70 entries — group by axis)
- When to capture vs search vs both
- TOM cards = volatile context (expires); capsules = period summaries; memory_items = canonical; revisions = evolution history
- Tone-shift implications: health/people_comms → warmth↑; work → directness↑; learning → evidence↑
- Privacy/quarantine: when to set status=quarantined instead of delete
- Tool catalog (signatures only, hermes auto-injects schemas)

## Soul (`~/.hermes/SOUL.md`)

Invariant identity. No tone language. ~30-60 lines. Drafted from aeon's `CHAT_SYSTEM_PROMPT` (in `aeon-stratum/apps/server/src/ai/prompts/chat.ts`) with the runtime/persona-customization bits aligned to hermes's prompt assembly.

## Build order

1. **Bootstrap repo** (task #1) — plugin.yaml, register stub, dir tree, pyproject
2. **SOUL.md** (task #2) — independent, can write any time
3. **Schema** (task #4) — schema.sql, db.py, smoke-run schema against libSQL local
4. **Provider** (task #5) — provider.py with stubs, then prefetch + system_prompt_block first (cheapest to verify)
5. **jina provider** (task #6) — quick, unblocks capture_note
6. **Tools** (task #7) — search/capture/calendar/set_tone/get_tone
7. **Tone codec** (task #3) — codec.py, hook.py, state.py; can land in parallel after #1
8. **Skill** (task #8) — write last, easier once tool signatures are concrete
9. **Smoke test** (task #9) — end-to-end via real hermes session

## Smoke test acceptance

- `hermes` boots with plugin loaded; no "single provider" warning (no other external provider configured)
- `capture_note(url="https://example.com/article")` → libSQL row + embedding + jina-extracted content
- `search_aeon(query="...", domain="learning")` → returns the captured row with RRF score
- Mid-session `set_tone(preset="analyst", weight=0.7)` → next assistant turn observably more direct/evidence-led
- Quit session → on_session_end fires, transcript scanned, capturable items appear in `memory_items`
- Restart hermes → SOUL.md + active TOM cards present in system prompt

---

Open questions for during build (not blocking):
- Embedding provider: Gemini text-embedding-004 (aeon's pick) vs OpenAI text-embedding-3-small vs local sentence-transformers. Affects schema dim and config.
- Turso sync: opt-in via env var (`HERMES_AEON_TURSO_URL` + `HERMES_AEON_TURSO_TOKEN`) or always-on? Defer to first-run config.
- LLM for on_session_end extraction: reuse the agent's primary model or call a cheap one. Probably cheap (haiku-tier).
