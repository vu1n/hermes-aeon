#!/usr/bin/env python3
"""Bounded stdio MCP access to a source-filtered projection of the Hermes Aeon KB.

Never open the original Aeon database here. The projection is a view of the
authoritative store, not an independently writable memory system.
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

SCOPE = "aeon-source-filtered-research-v1"
DOMAINS = {"learning", "work", "side_projects"}
SOURCES = {"x-bookmark", "discover:hf-papers", "discover:hn", "discover:hype-github",
           "discover:hype-huggingface", "discover:hype-reddit", "discover:lobsters", "discover:x",
           "github:ForkEvent", "github:IssueCommentEvent", "github:IssuesEvent",
           "github:PullRequestEvent", "github:PullRequestReviewCommentEvent",
           "github:PullRequestReviewEvent", "github:WatchEvent"}
HOSTS = {"github.com", "arxiv.org", "huggingface.co", "news.ycombinator.com",
         "lobste.rs", "x.com", "twitter.com", "reddit.com", "www.reddit.com"}
UNSAFE = re.compile(
    r"(?i)\b(?:health|medical|patient|diagnos\w*|therapy|sleep|nutrition|oura|"
    r"multivitamin|medication|prescription|salary|bank|mortgage|credit\s*card|"
    r"investment|financial|password|credential|secret|bearer|private[ _-]?key)\b|"
    r"api[ _-]?key|\bsk-[A-Za-z0-9_-]+|\btoken\s*[:=]|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|"
    r"(?:reveal|exfiltrate)\s+(?:the\s+)?(?:secret|token|credential)"
)
FIELDS = ("id", "source", "domain", "type", "status", "captured_at", "current_revision",
          "title", "summary", "content", "url", "screened_at", "scope_version")
SELECT = ",".join(FIELDS)
BASE = "status='active' AND scope_version=?"
ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False,
               "idempotentHint": True, "openWorldHint": False}


class Invalid(ValueError):
    pass

class Unavailable(RuntimeError):
    pass


def bounded_int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise Invalid("Integer outside allowed range")
    return value


def arguments(args, allowed):
    if not isinstance(args, dict) or set(args) - set(allowed):
        raise Invalid("Unexpected tool arguments")


class Store:
    def __init__(self, path):
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise Invalid("Projection is unavailable")
        self.path = path
        self.signature = (path.stat().st_dev, path.stat().st_ino, path.stat().st_mtime_ns)
        # mode=ro participates safely in SQLite snapshots. Never use immutable=1
        # while an external publisher might replace/update the projection.
        self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        self.db.execute("PRAGMA trusted_schema=OFF")
        self.deadline = 0
        self.db.set_progress_handler(lambda: int(time.monotonic() > self.deadline), 1000)
        self.deadline = time.monotonic() + 1
        scope = self.db.execute("SELECT value FROM projection_metadata WHERE key='scope_version'").fetchone()
        if scope is None or scope[0] != SCOPE:
            raise Invalid("Source-filtered projection scope is required")
        timestamp = self.db.execute("SELECT value FROM projection_metadata WHERE key='published_at_ms'").fetchone()
        if timestamp is None: raise Unavailable("Publication timestamp required")
        self.published_at = int(timestamp[0])
        self.db.execute("SELECT " + SELECT + " FROM approved_memories LIMIT 0")

    def refresh(self):
        stat = self.path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns)
        if signature == self.signature:
            return
        replacement = Store(self.path)
        self.db.close()
        self.db = replacement.db
        self.signature = replacement.signature
        self.published_at = replacement.published_at
        # Install a callback bound to this persistent Store instance.
        self.db.set_progress_handler(lambda: int(time.monotonic() > self.deadline), 1000)

    def rows(self, sql, params):
        self.deadline = time.monotonic() + 0.5
        rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def safe(row, full=False):
        if row["source"] not in SOURCES or row["domain"] not in DOMAINS:
            return None
        if row["status"] != "active" or row["type"] not in {"link", "note"}:
            return None
        if row["scope_version"] != SCOPE or type(row["screened_at"]) is not int or row["screened_at"] < 1:
            return None
        if not re.fullmatch(r"[a-f0-9]{32}", row["id"] or ""):
            return None
        for field in ("captured_at", "current_revision"):
            if type(row[field]) is not int or row[field] < 1:
                return None
        texts = [row[k] or "" for k in ("title", "summary", "content", "url")]
        if any(not isinstance(t, str) or len(t) > 100_000 for t in texts):
            return None
        if UNSAFE.search("\n".join(texts)):
            return None
        url = row["url"]
        if url:
            try:
                parsed = urlsplit(url)
                if (parsed.scheme != "https" or parsed.hostname not in HOSTS or
                    parsed.username or parsed.password or parsed.query or parsed.fragment or
                    parsed.port not in (None, 443)):
                    return None
            except ValueError:
                return None
        result = {k: row[k] for k in FIELDS if k not in {"scope_version", "content"}}
        result["title"] = (row["title"] or "")[:300]
        result["summary"] = (row["summary"] or "")[:2000]
        if full:
            result["content"] = (row["content"] or "")[:12_000]
            result["content_truncated"] = len(row["content"] or "") > 12_000
        else:
            result["excerpt"] = (row["summary"] or row["content"] or "")[:1000]
        return result

    def call(self, name, args):
        self.refresh()
        age = int(time.time()*1000) - self.published_at
        if not -60_000 <= age <= 900_000: raise Unavailable("Projection freshness expired")
        if name == "aeon_get":
            arguments(args, {"id"})
            rid = args.get("id")
            if not isinstance(rid, str) or not re.fullmatch(r"[a-f0-9]{32}", rid):
                raise Invalid("Expected a source memory ID")
            rows = self.rows("SELECT " + SELECT + " FROM approved_memories WHERE " + BASE + " AND id=?", (SCOPE, rid))
            item = self.safe(rows[0], full=True) if rows else None
            # Missing and excluded records are intentionally indistinguishable.
            return {"item": item}
        if name not in {"aeon_search", "aeon_recent"}:
            raise Invalid("Unknown tool")
        arguments(args, {"query", "domain", "limit"} if name == "aeon_search" else {"hours", "domain", "limit"})
        limit = bounded_int(args.get("limit", 10), 1, 20)
        where, params = [BASE], [SCOPE]
        domain = args.get("domain")
        if domain is not None:
            if not isinstance(domain, str) or domain not in DOMAINS:
                raise Invalid("Domain outside allowed scope")
            where.append("domain=?"); params.append(domain)
        if name == "aeon_search":
            query = args.get("query")
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
                raise Invalid("Query must contain 1 to 256 characters")
            tokens = re.findall(r"[\w-]+", query, flags=re.UNICODE)
            if not tokens or len(tokens) > 8 or any(len(t) > 64 for t in tokens):
                raise Invalid("Query must contain 1 to 8 bounded terms")
            for token in tokens:
                token = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where.append("(coalesce(title,'') || ' ' || coalesce(summary,'') || ' ' || coalesce(content,'')) LIKE ? ESCAPE '\\'")
                params.append("%" + token + "%")
        else:
            hours = bounded_int(args.get("hours", 24), 1, 2160)
            where.append("captured_at>=?"); params.append(int(time.time()*1000)-hours*3_600_000)
        params.append(200)
        rows = self.rows("SELECT " + SELECT + " FROM approved_memories WHERE " + " AND ".join(where) + " ORDER BY captured_at DESC,id ASC LIMIT ?", params)
        items = [item for r in rows if (item := self.safe(r)) is not None]
        return {"items": items[:limit], "count": min(len(items), limit),
                "candidate_window_exhausted": len(rows) == 200,
                "provenance_note": "Retrieved source material; source and active status do not establish user authorship, endorsement, or currentness."}


def tools():
    domain = {"type": "string", "enum": sorted(DOMAINS)}
    limit = {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}
    definitions = [
        ("aeon_search", "Search source-filtered Aeon research/bookmark/GitHub records. All terms must match; newest first.",
         {"query": {"type": "string", "minLength": 1, "maxLength": 256}, "domain": domain, "limit": limit}, ["query"]),
        ("aeon_recent", "Read recent source-filtered Aeon records with original provenance.",
         {"hours": {"type": "integer", "minimum": 1, "maximum": 2160, "default": 24}, "domain": domain, "limit": limit}, []),
        ("aeon_get", "Fetch one source-filtered source record by ID; excluded and missing IDs return null.",
         {"id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}, ["id"])]
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p,
             "required": r, "additionalProperties": False}, "annotations": ANNOTATIONS} for n,d,p,r in definitions]


def serve(store, stdin=sys.stdin.buffer, stdout=sys.stdout):
    initialized = False
    def emit(value):
        stdout.write(json.dumps(value, ensure_ascii=True) + "\n"); stdout.flush()
    while True:
        raw = stdin.readline(65_537)
        if not raw:
            return
        if len(raw) > 65_536:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request exceeds limit"}})
            return  # Do not interpret the remainder of an oversized request.
        rid = None
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise Invalid("Invalid JSON-RPC request")
            if "id" not in request:
                continue
            rid = request["id"]
            if type(rid) not in (str, int) or (isinstance(rid, str) and len(rid) > 64):
                rid = None; raise Invalid("Invalid request ID")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise Invalid("Invalid parameters")
            if method == "initialize":
                version = params.get("protocolVersion")
                if version not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
                    version = "2025-03-26"
                result = {"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "aeon-shared-brain", "version": "1.0.0"}}
                initialized = True
            elif method == "ping":
                result = {}
            elif not initialized:
                raise Invalid("Initialize first")
            elif method == "tools/list":
                result = {"tools": tools()}
            elif method == "tools/call":
                value = store.call(params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value, "isError": False}
            else:
                emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}); continue
            emit({"jsonrpc": "2.0", "id": rid, "result": result})
        except json.JSONDecodeError:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid request or tool arguments"}})
        except (sqlite3.Error, OSError, Unavailable):
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Read unavailable or query exceeded budget"}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Source-filtered read-only projection, never the original Hermes DB")
    args = parser.parse_args()
    try:
        store = Store(args.db)
    except (OSError, Invalid, Unavailable, sqlite3.Error):
        print("Source-filtered projection unavailable; refusing to start", file=sys.stderr)
        return 1
    serve(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
