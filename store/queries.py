"""Typed query layer. JSON columns hydrated at the boundary — callers never see raw JSON."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from utils import safe_json_loads

from .db import AeonDB, emb_to_libsql_literal

logger = logging.getLogger(__name__)


VALID_DOMAINS = {"inbox", "work", "side_projects", "learning", "health", "life_admin", "people_comms"}
VALID_TYPES = {"note", "link", "email", "thread", "task", "health_log", "file", "calendar_event", "contact", "other"}
VALID_STATUSES = {"active", "archived", "quarantined"}


@dataclass
class MemoryItem:
    id: str
    type: str
    domain: str
    status: str = "active"
    title: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    url: Optional[str] = None
    entities: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    summary_bullets: list = field(default_factory=list)
    project_id: Optional[str] = None
    quality_score: Optional[float] = None
    source: Optional[str] = None
    captured_at: int = 0
    event_start: Optional[int] = None
    event_end: Optional[int] = None
    accessed_at: Optional[int] = None
    access_count: int = 0
    user_id: str = "local"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type, "domain": self.domain, "status": self.status,
            "title": self.title, "summary": self.summary, "content": self.content,
            "url": self.url, "entities": self.entities, "tags": self.tags,
            "summary_bullets": self.summary_bullets, "project_id": self.project_id,
            "quality_score": self.quality_score, "source": self.source,
            "captured_at": self.captured_at, "event_start": self.event_start,
            "event_end": self.event_end, "accessed_at": self.accessed_at,
            "access_count": self.access_count,
        }


def _hydrate_row(row: tuple) -> MemoryItem:
    (id_, user_id, type_, domain, status, title, summary, content, url,
     entities, tags, summary_bullets, project_id, quality_score, source,
     captured_at, event_start, event_end, accessed_at, access_count, *_rest) = row
    return MemoryItem(
        id=id_, user_id=user_id, type=type_, domain=domain, status=status,
        title=title, summary=summary, content=content, url=url,
        entities=safe_json_loads(entities, default={}) or {},
        tags=safe_json_loads(tags, default=[]) or [],
        summary_bullets=safe_json_loads(summary_bullets, default=[]) or [],
        project_id=project_id, quality_score=quality_score, source=source,
        captured_at=captured_at, event_start=event_start, event_end=event_end,
        accessed_at=accessed_at, access_count=access_count or 0,
    )


_SELECT_COLS = ("id, user_id, type, domain, status, title, summary, content, url, "
                "entities, tags, summary_bullets, project_id, quality_score, source, "
                "captured_at, event_start, event_end, accessed_at, access_count")


def now_ms() -> int:
    return int(time.time() * 1000)


def capture_memory(
    db: AeonDB,
    *,
    type: str,
    domain: str,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    content: Optional[str] = None,
    url: Optional[str] = None,
    tags: Optional[list] = None,
    entities: Optional[dict] = None,
    project_id: Optional[str] = None,
    source: Optional[str] = None,
    event_start: Optional[int] = None,
    event_end: Optional[int] = None,
    dedup_key: Optional[str] = None,
    embedding: Optional[list[float]] = None,
    embedding_model: Optional[str] = None,
) -> str:
    """Insert a memory_item + first revision + optional embedding. Returns the new id."""
    if domain not in VALID_DOMAINS:
        raise ValueError(f"unknown domain: {domain}")
    if type not in VALID_TYPES:
        raise ValueError(f"unknown type: {type}")

    if dedup_key:
        existing = db.execute(
            "SELECT id FROM memory_items WHERE dedup_key = ? LIMIT 1", (dedup_key,)
        ).fetchone()
        if existing:
            return existing[0]

    mid = uuid.uuid4().hex
    ts = now_ms()
    db.execute(
        """INSERT INTO memory_items
           (id, user_id, type, domain, status, title, summary, content, url,
            entities, tags, summary_bullets, project_id, source,
            captured_at, event_start, event_end, dedup_key, current_revision)
           VALUES (?, 'local', ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (mid, type, domain, title, summary, content, url,
         json.dumps(entities or {}), json.dumps(tags or []), json.dumps([]),
         project_id, source, ts, event_start, event_end, dedup_key),
    )
    db.execute(
        "INSERT INTO memory_revisions (memory_id, revision_n, content, summary, source, created_at) VALUES (?, 1, ?, ?, ?, ?)",
        (mid, content, summary, source, ts),
    )
    db.execute(
        "INSERT INTO memory_fts (memory_id, title, summary, content) VALUES (?, ?, ?, ?)",
        (mid, title or "", summary or "", content or ""),
    )
    if embedding and db.has_vector:
        db.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, model, embedded_at) VALUES (?, vector32(?), ?, ?)",
            (mid, emb_to_libsql_literal(embedding), embedding_model or "unknown", ts),
        )
    db.commit()
    return mid


def update_memory_content(
    db: AeonDB, *, memory_id: str, content: Optional[str], summary: Optional[str], source: Optional[str],
    embedding: Optional[list[float]] = None, embedding_model: Optional[str] = None,
) -> int:
    """Append a revision. Returns the new revision number."""
    row = db.execute(
        "SELECT current_revision, title FROM memory_items WHERE id = ?", (memory_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"memory_id not found: {memory_id}")
    next_rev = (row[0] or 1) + 1
    title = row[1]
    ts = now_ms()
    db.execute(
        "INSERT INTO memory_revisions (memory_id, revision_n, content, summary, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (memory_id, next_rev, content, summary, source, ts),
    )
    db.execute(
        "UPDATE memory_items SET content = ?, summary = ?, current_revision = ? WHERE id = ?",
        (content, summary, next_rev, memory_id),
    )
    db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
    db.execute(
        "INSERT INTO memory_fts (memory_id, title, summary, content) VALUES (?, ?, ?, ?)",
        (memory_id, title or "", summary or "", content or ""),
    )
    if embedding and db.has_vector:
        db.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
        db.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, model, embedded_at) VALUES (?, vector32(?), ?, ?)",
            (memory_id, emb_to_libsql_literal(embedding), embedding_model or "unknown", ts),
        )
    db.commit()
    return next_rev


def search_memories(
    db: AeonDB, *, query: str, embedding: Optional[list[float]] = None,
    domain: Optional[str] = None, type: Optional[str] = None, project_id: Optional[str] = None,
    limit: int = 10,
) -> list[MemoryItem]:
    """Hybrid retrieval: vector_distance_cos + FTS5 bm25 fused via reciprocal-rank fusion.

    Pure read — callers must explicitly call ``touch_memories`` to bump access counts.
    """
    fts_results: list[tuple[str, int]] = []
    try:
        fts_query = " OR ".join(t for t in query.split() if t)
        if fts_query:
            rows = db.execute(
                "SELECT memory_id, bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 50",
                (fts_query,),
            ).fetchall()
            fts_results = [(r[0], i) for i, r in enumerate(rows)]
    except Exception as e:
        logger.debug("fts search failed: %s", e)

    vec_results: list[tuple[str, int]] = []
    if embedding and db.has_vector:
        try:
            rows = db.execute(
                "SELECT memory_id, vector_distance_cos(embedding, vector32(?)) AS dist FROM memory_embeddings ORDER BY dist LIMIT 50",
                (emb_to_libsql_literal(embedding),),
            ).fetchall()
            vec_results = [(r[0], i) for i, r in enumerate(rows)]
        except Exception as e:
            logger.debug("vector search failed: %s", e)

    K = 60
    scores: dict[str, float] = {}
    for mid, rank in fts_results:
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (K + rank)
    for mid, rank in vec_results:
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (K + rank)

    if not scores:
        return []

    placeholders = ",".join("?" for _ in scores)
    where = ["status = 'active'", f"id IN ({placeholders})"]
    params: list = list(scores.keys())
    if domain:
        where.append("domain = ?"); params.append(domain)
    if type:
        where.append("type = ?"); params.append(type)
    if project_id:
        where.append("project_id = ?"); params.append(project_id)

    rows = db.execute(
        f"SELECT {_SELECT_COLS} FROM memory_items WHERE {' AND '.join(where)}",
        tuple(params),
    ).fetchall()

    items = [_hydrate_row(r) for r in rows]
    items.sort(key=lambda it: scores.get(it.id, 0.0), reverse=True)
    return items[:limit]


def touch_memories(db: AeonDB, memory_ids: list[str]) -> None:
    """Bump access_count + accessed_at. Separate from search_memories so reads stay pure."""
    if not memory_ids:
        return
    ph = ",".join("?" for _ in memory_ids)
    db.execute(
        f"UPDATE memory_items SET access_count = access_count + 1, accessed_at = ? WHERE id IN ({ph})",
        (now_ms(), *memory_ids),
    )
    db.commit()


def get_calendar(
    db: AeonDB, *, start_ms: int, end_ms: int, domain: Optional[str] = None, limit: int = 50,
) -> list[MemoryItem]:
    where = ["status = 'active'", "type = 'calendar_event'", "event_start >= ?", "event_start <= ?"]
    params: list = [start_ms, end_ms]
    if domain:
        where.append("domain = ?"); params.append(domain)
    rows = db.execute(
        f"SELECT {_SELECT_COLS} FROM memory_items WHERE {' AND '.join(where)} ORDER BY event_start ASC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_hydrate_row(r) for r in rows]


def active_tom_cards(db: AeonDB, *, domain: Optional[str] = None, limit: int = 20) -> list[dict]:
    where = ["expires_at > ?"]
    params: list = [now_ms()]
    if domain:
        where.append("domain = ?"); params.append(domain)
    rows = db.execute(
        f"SELECT id, domain, content, expires_at, created_at FROM tom_cards WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [{"id": r[0], "domain": r[1], "content": r[2], "expires_at": r[3], "created_at": r[4]} for r in rows]


def recent_capsules(db: AeonDB, *, period: str = "daily", domain: Optional[str] = None, limit: int = 5) -> list[dict]:
    where = ["period = ?"]
    params: list = [period]
    if domain:
        where.append("domain = ?"); params.append(domain)
    rows = db.execute(
        f"SELECT id, domain, period, period_start, period_end, summary FROM capsules WHERE {' AND '.join(where)} ORDER BY period_start DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [{"id": r[0], "domain": r[1], "period": r[2], "start": r[3], "end": r[4], "summary": r[5]} for r in rows]
