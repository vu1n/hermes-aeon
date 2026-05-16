-- hermes-aeon schema. Targets libSQL (native vector + FTS5).
-- Falls back to FTS5-only if vector_distance_cos isn't available.

CREATE TABLE IF NOT EXISTS memory_items (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL DEFAULT 'local',
  type              TEXT NOT NULL,
  domain            TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'active',
  title             TEXT,
  summary           TEXT,
  content           TEXT,
  url               TEXT,
  entities          TEXT,                       -- JSON dict
  tags              TEXT,                       -- JSON array
  summary_bullets   TEXT,                       -- JSON array
  project_id        TEXT,
  quality_score     REAL,
  source            TEXT,
  captured_at       INTEGER NOT NULL,
  event_start       INTEGER,
  event_end         INTEGER,
  accessed_at       INTEGER,
  access_count      INTEGER NOT NULL DEFAULT 0,
  dedup_key         TEXT,
  artifact_sha      TEXT,
  current_revision  INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_memory_domain_type ON memory_items(domain, type, status);
CREATE INDEX IF NOT EXISTS idx_memory_project ON memory_items(project_id);
CREATE INDEX IF NOT EXISTS idx_memory_captured ON memory_items(captured_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedup ON memory_items(dedup_key) WHERE dedup_key IS NOT NULL;

-- Append-only revisions for concept evolution
CREATE TABLE IF NOT EXISTS memory_revisions (
  memory_id   TEXT NOT NULL,
  revision_n  INTEGER NOT NULL,
  content     TEXT,
  summary     TEXT,
  source      TEXT,
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (memory_id, revision_n)
);

-- libSQL native vectors. F32_BLOB(N) is libSQL-specific.
-- If running pure SQLite, this CREATE will fail; provider degrades to FTS5-only.
CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id   TEXT PRIMARY KEY,
  embedding   F32_BLOB(768) NOT NULL,
  model       TEXT NOT NULL,
  embedded_at INTEGER NOT NULL
);

-- Volatile/expiring TOM cards (per-domain rolling context)
CREATE TABLE IF NOT EXISTS tom_cards (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL DEFAULT 'local',
  domain      TEXT NOT NULL,
  content     TEXT NOT NULL,
  expires_at  INTEGER NOT NULL,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tom_domain_expires ON tom_cards(domain, expires_at);

-- Period-bounded summaries
CREATE TABLE IF NOT EXISTS capsules (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL DEFAULT 'local',
  domain        TEXT NOT NULL,
  period        TEXT NOT NULL,
  period_start  INTEGER NOT NULL,
  period_end    INTEGER NOT NULL,
  summary       TEXT NOT NULL,
  created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capsules_domain_period ON capsules(domain, period, period_start);

-- Hybrid lexical search
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  memory_id UNINDEXED,
  title,
  summary,
  content,
  tokenize = 'porter unicode61'
);
