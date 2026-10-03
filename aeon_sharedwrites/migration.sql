-- Explicit additive migration. Never run implicitly by a request or import.
CREATE TABLE IF NOT EXISTS memory_write_requests (
 consumer_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, result_json TEXT NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(consumer_id,request_id)
);
CREATE TABLE IF NOT EXISTS memory_write_audit (
 audit_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, revision_n INTEGER NOT NULL,
 consumer_id TEXT NOT NULL, operation TEXT NOT NULL, entry_kind TEXT NOT NULL,
 attribution_basis TEXT NOT NULL, conversation_ref TEXT, created_at INTEGER NOT NULL,
 UNIQUE(memory_id,revision_n)
);
CREATE INDEX IF NOT EXISTS memory_write_actor ON memory_write_audit(consumer_id,created_at);
