"""Typed query layer. JSON columns hydrated at the boundary — callers never see raw JSON."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .utils import safe_json_loads
from . import shared_writer
if '.' in __package__:
    from ..brain_service import hermes as general
else:
    from brain_service import hermes as general

from .db import AeonDB

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
    current_revision: int = 1

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type, "domain": self.domain, "status": self.status,
            "title": self.title, "summary": self.summary, "content": self.content,
            "url": self.url, "entities": self.entities, "tags": self.tags,
            "summary_bullets": self.summary_bullets, "project_id": self.project_id,
            "quality_score": self.quality_score, "source": self.source,
            "captured_at": self.captured_at, "event_start": self.event_start,
            "event_end": self.event_end, "accessed_at": self.accessed_at,
            "access_count": self.access_count, "current_revision": self.current_revision,
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
        current_revision=_rest[0] if _rest else 1,
    )


_SELECT_COLS = ("id, user_id, type, domain, status, title, summary, content, url, "
                "entities, tags, summary_bullets, project_id, quality_score, source, "
                "captured_at, event_start, event_end, accessed_at, access_count, current_revision")


def now_ms() -> int:
    return int(time.time() * 1000)


def capture_memory(db,*,type,domain,title=None,summary=None,content=None,url=None,tags=None,
    entities=None,project_id=None,source=None,event_start=None,event_end=None,dedup_key=None,
    quality_score=None,captured_at=None,embedding=None,embedding_model=None,request_id=None):
    return shared_writer.capture(db,consumer_id='hermes',request_id=request_id or uuid.uuid4().hex,
        type=type,domain=domain,title=title,summary=summary,content=content,url=url,tags=tags,
        entities=entities,project_id=project_id,source=source,event_start=event_start,event_end=event_end,
        dedup_key=dedup_key,quality_score=quality_score,captured_at=captured_at,embedding=embedding,
        embedding_model=embedding_model,revision_hook=general.revision_hook(db))['memory_id']

def update_memory_content(db,*,memory_id,expected_revision,content,summary,source,
    embedding=None,embedding_model=None,request_id=None):
    return shared_writer.update(db,consumer_id='hermes',request_id=request_id or uuid.uuid4().hex,
        memory_id=memory_id,expected_revision=expected_revision,content=content,summary=summary,
        source=source,embedding=embedding,embedding_model=embedding_model,revision_hook=general.revision_hook(db))['revision']


def search_memories(
    db: AeonDB, *, query: str, embedding: Optional[list[float]] = None,
    domain: Optional[str] = None, type: Optional[str] = None, project_id: Optional[str] = None,
    limit: int = 10,
) -> list[MemoryItem]:
    """General-only lexical recall; global vector/FTS candidates are never consulted."""
    result=general.service(db).search(general.HERMES,query=query,domain=domain,type=type,
                                     project_id=project_id,limit=limit)
    return [MemoryItem(**{k:v for k,v in item.items() if k in MemoryItem.__dataclass_fields__}) for item in result['items']]


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
