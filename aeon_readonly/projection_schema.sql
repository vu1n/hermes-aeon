-- Only a trusted publisher may create this database. The MCP reader never
-- creates tables, writes rows, imports provider code, or opens the source DB.
CREATE TABLE projection_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO projection_metadata VALUES('scope_version','aeon-source-filtered-research-v1');
INSERT INTO projection_metadata VALUES('published_at_ms',CAST(strftime('%s','now') AS INTEGER)*1000);
CREATE TABLE approved_memories(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, domain TEXT NOT NULL,
 type TEXT NOT NULL, status TEXT NOT NULL, captured_at INTEGER NOT NULL,
 current_revision INTEGER NOT NULL, title TEXT, summary TEXT, content TEXT,
 url TEXT, screened_at INTEGER NOT NULL, scope_version TEXT NOT NULL
);
CREATE INDEX approved_recent ON approved_memories(status,scope_version,captured_at DESC);
CREATE INDEX approved_domain_recent ON approved_memories(domain,status,captured_at DESC);
