-- Explicit additive foundation schema. Unknown legacy records are not admitted.
CREATE TABLE IF NOT EXISTS brain_records (
 memory_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, creator TEXT NOT NULL,
 current_revision INTEGER NOT NULL, sensitivity TEXT NOT NULL,
 record_class TEXT NOT NULL, kind TEXT NOT NULL, verification TEXT NOT NULL,
 topics_json TEXT NOT NULL, evidence_json TEXT NOT NULL, applicability TEXT,
 expires_at INTEGER, valid INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS brain_revision_metadata (
 memory_id TEXT NOT NULL, revision_n INTEGER NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL, snapshot_json TEXT NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(memory_id,revision_n)
);
CREATE TABLE IF NOT EXISTS brain_links (
 memory_id TEXT NOT NULL, revision_n INTEGER NOT NULL,
 target_id TEXT NOT NULL, target_revision INTEGER NOT NULL, relationship TEXT NOT NULL,
 PRIMARY KEY(memory_id,revision_n,target_id,target_revision,relationship)
);
CREATE TABLE IF NOT EXISTS brain_general_changes (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL,
 revision_n INTEGER NOT NULL, operation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brain_general_index (
 memory_id TEXT PRIMARY KEY, revision_n INTEGER NOT NULL, document_json TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS brain_general_fts USING fts5(
 memory_id UNINDEXED,title,summary,content,topics,tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS brain_requests (
 principal TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
 result_json TEXT NOT NULL, PRIMARY KEY(principal,request_id)
);

CREATE TABLE IF NOT EXISTS brain_review_attempts (
 attempt_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory_items(id),
 parent_attempt TEXT REFERENCES brain_review_attempts(attempt_id), creator TEXT NOT NULL,
 candidate_id TEXT NOT NULL, pins_json TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS brain_review_decisions (
 attempt_id TEXT PRIMARY KEY REFERENCES brain_review_attempts(attempt_id),
 reviewer TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
 reason_code TEXT NOT NULL, expected_revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
 UNIQUE(reviewer,request_id)
);
