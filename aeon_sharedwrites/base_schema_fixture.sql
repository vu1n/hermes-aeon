-- Synthetic fixture matching verified live columns/indexes; never deployed.
CREATE TABLE memory_items(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL DEFAULT 'local',type TEXT NOT NULL,
 domain TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',title TEXT,summary TEXT,
 content TEXT,url TEXT,entities TEXT,tags TEXT,summary_bullets TEXT,project_id TEXT,
 quality_score REAL,source TEXT,captured_at INTEGER NOT NULL,event_start INTEGER,
 event_end INTEGER,accessed_at INTEGER,access_count INTEGER NOT NULL DEFAULT 0,
 dedup_key TEXT,artifact_sha TEXT,current_revision INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX idx_memory_dedup ON memory_items(dedup_key) WHERE dedup_key IS NOT NULL;
CREATE TABLE memory_revisions(memory_id TEXT NOT NULL,revision_n INTEGER NOT NULL,
 content TEXT,summary TEXT,source TEXT,created_at INTEGER NOT NULL,
 PRIMARY KEY(memory_id,revision_n));
CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED,title,summary,content,tokenize='porter unicode61');
