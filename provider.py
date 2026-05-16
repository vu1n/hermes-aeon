"""AeonMemoryProvider — bundles personal-KB recall, tone codec, and auto-capture for Hermes."""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_home
from tools.registry import tool_error, tool_result
from utils import atomic_yaml_write

from .store.db import AeonDB
from .store import queries as q
from .store.embed import embed_text
from .tone.codec import AXES, PRESETS, apply_axes, apply_preset
from .tone.state import ToneStore
from .tools.browser_providers import jina

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

DOMAINS = sorted(q.VALID_DOMAINS)
TYPES = sorted(q.VALID_TYPES)
TONE_PRESET_NAMES = sorted(PRESETS.keys())

_DOMAIN_FIELD = {"type": "string", "enum": DOMAINS}
_TYPE_FIELD = {"type": "string", "enum": TYPES}

CAPTURE_SCHEMA = {
    "name": "aeon_capture",
    "description": (
        "Save a memory to the personal knowledge base. Use for notes, links, "
        "calendar events, contacts, health logs, files. Pass `url` to extract "
        "and store a webpage; pass `content` for raw text. Always picks a domain "
        "and type — no inbox dumping."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "domain": _DOMAIN_FIELD,
            "type": _TYPE_FIELD,
            "content": {"type": "string", "description": "Raw text. Required if url is absent."},
            "url": {"type": "string", "description": "If present, extracted via configured extractor."},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "project_id": {"type": "string"},
            "event_start_ms": {"type": "integer", "description": "Unix ms; for calendar_event."},
            "event_end_ms": {"type": "integer"},
        },
        "required": ["domain", "type"],
    },
}

SEARCH_SCHEMA = {
    "name": "aeon_search",
    "description": "Hybrid vector + FTS5 search over the personal KB. Filter by domain/type/project_id.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "domain": _DOMAIN_FIELD,
            "type": _TYPE_FIELD,
            "project_id": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
}

CALENDAR_SCHEMA = {
    "name": "aeon_calendar",
    "description": "Calendar events between start_ms and end_ms (Unix ms). Filter by domain.",
    "parameters": {
        "type": "object",
        "properties": {
            "start_ms": {"type": "integer"},
            "end_ms": {"type": "integer"},
            "domain": _DOMAIN_FIELD,
            "limit": {"type": "integer", "default": 50},
        },
        "required": ["start_ms", "end_ms"],
    },
}

UPDATE_SCHEMA = {
    "name": "aeon_update",
    "description": "Append a revision to an existing memory. Tracks how a concept evolves over time.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string"},
            "content": {"type": "string"},
            "summary": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["memory_id", "content"],
    },
}

SET_TONE_SCHEMA = {
    "name": "aeon_set_tone",
    "description": (
        "Adjust the agent's voice for this session. Pass a preset name OR explicit axes "
        "(any subset of warmth/directness/structure/evidence/playfulness on [0,1]). "
        "Weight controls how strongly to blend toward the target (0 = no change, 1 = replace)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "preset": {"type": "string", "enum": TONE_PRESET_NAMES},
            "axes": {
                "type": "object",
                "properties": {a: {"type": "number"} for a in AXES},
            },
            "weight": {"type": "number", "default": 0.7},
        },
    },
}

GET_TONE_SCHEMA = {
    "name": "aeon_get_tone",
    "description": "Return the current 5-axis tone state for this session.",
    "parameters": {"type": "object", "properties": {}},
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

def _load_plugin_config() -> dict:
    try:
        import yaml
        cfg_path = get_hermes_home() / "config.yaml"
        if not cfg_path.exists():
            return {}
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig")) or {}
        return cfg_get(data, "plugins", "hermes-aeon", default={}) or {}
    except Exception:
        return {}


class AeonMemoryProvider(MemoryProvider):
    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_plugin_config()
        self._db: Optional[AeonDB] = None
        self._tone: Optional[ToneStore] = None
        self._session_id: str = ""
        self._hermes_home: Path = get_hermes_home()
        self._embed_provider: str = self._config.get("embed_provider", "gemini")
        # Only jina is implemented today; firecrawl support would land alongside its provider wrapper.
        self._extractor: str = self._config.get("extractor", "jina")
        self._auto_extract: bool = bool(self._config.get("auto_extract", True))
        self._handlers: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "hermes-aeon"

    def is_available(self) -> bool:
        return True

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "db_path", "description": "libSQL database path", "default": "$HERMES_HOME/aeon.db"},
            {"key": "embed_provider", "description": "Embedding backend", "default": "gemini", "choices": ["gemini", "none"]},
            {"key": "extractor", "description": "URL content extractor", "default": "jina", "choices": ["jina"]},
            {"key": "auto_extract", "description": "Auto-extract memories on session end", "default": "true", "choices": ["true", "false"]},
            {"key": "turso_url", "description": "Turso sync URL (optional)", "secret": False, "env_var": "HERMES_AEON_TURSO_URL"},
            {"key": "turso_token", "description": "Turso auth token", "secret": True, "env_var": "HERMES_AEON_TURSO_TOKEN"},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        try:
            import yaml
            cfg_path = Path(hermes_home) / "config.yaml"
            existing = {}
            if cfg_path.exists():
                existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig")) or {}
            existing.setdefault("plugins", {})
            existing["plugins"]["hermes-aeon"] = values
            atomic_yaml_write(cfg_path, existing)
        except Exception as e:
            logger.warning("save_config failed: %s", e)

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = Path(kwargs.get("hermes_home") or get_hermes_home())
        self._hermes_home = hermes_home
        db_path = self._config.get("db_path", str(hermes_home / "aeon.db"))
        if isinstance(db_path, str):
            db_path = db_path.replace("$HERMES_HOME", str(hermes_home))
            db_path = db_path.replace("${HERMES_HOME}", str(hermes_home))

        self._db = AeonDB(
            db_path=db_path,
            turso_url=self._config.get("turso_url"),
            turso_token=self._config.get("turso_token"),
        )
        self._db.connect()
        self._db.bootstrap_schema()

        tone_path = hermes_home / "hermes-aeon" / "tone.json"
        self._tone = ToneStore(tone_path)
        self._session_id = session_id
        self._handlers = {
            "aeon_capture": self._handle_capture,
            "aeon_search": self._handle_search,
            "aeon_calendar": self._handle_calendar,
            "aeon_update": self._handle_update,
            "aeon_set_tone": self._handle_set_tone,
            "aeon_get_tone": self._handle_get_tone,
        }

    def system_prompt_block(self) -> str:
        if not self._db:
            return ""
        cards = q.active_tom_cards(self._db, limit=8)
        capsules = q.recent_capsules(self._db, period="daily", limit=3)
        if not cards and not capsules:
            return ""
        lines = ["## Aeon Memory"]
        if cards:
            lines.append("Active TOM cards (volatile context):")
            for c in cards:
                lines.append(f"- [{c['domain']}] {c['content']}")
        if capsules:
            lines.append("Recent daily capsules:")
            for c in capsules:
                lines.append(f"- [{c['domain']}] {c['summary']}")
        lines.append("Use aeon_search/aeon_capture/aeon_calendar to read and write the personal KB.")
        return "\n".join(lines)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._db or not query or len(query) < 4:
            return ""
        try:
            emb, _ = embed_text(query, provider=self._embed_provider)
            hits = q.search_memories(self._db, query=query, embedding=emb, limit=5)
            if not hits:
                return ""
            lines = ["Aeon memory recall:"]
            for h in hits:
                snippet = (h.summary or h.content or "")[:200].replace("\n", " ")
                lines.append(f"- [{h.domain}/{h.type}] {h.title or '(untitled)'}: {snippet}")
            return "\n".join(lines)
        except Exception as e:
            logger.debug("aeon prefetch search failed: %s", e)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # Heavy capture deferred to on_session_end; sync_turn stays cheap.
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            CAPTURE_SCHEMA, SEARCH_SCHEMA, CALENDAR_SCHEMA, UPDATE_SCHEMA,
            SET_TONE_SCHEMA, GET_TONE_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._db:
            return tool_error("aeon provider not initialized")
        handler = self._handlers.get(tool_name)
        if handler is None:
            return tool_error(f"unknown tool: {tool_name}")
        try:
            return handler(args)
        except KeyError as e:
            return tool_error(f"missing argument: {e}")
        except Exception as e:
            logger.exception("aeon tool error")
            return tool_error(str(e))

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._auto_extract or not self._db or not messages:
            return
        self._auto_extract_from_transcript(messages)
        self._db.sync()

    def shutdown(self) -> None:
        if self._db:
            self._db.sync()
            self._db.close()
        self._db = None

    # -- Tool handlers ------------------------------------------------------

    @staticmethod
    def _derive_title_summary(extracted: str, url: str, title: Optional[str], summary: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not title:
            first_line = next((ln for ln in extracted.splitlines() if ln.strip()), "")
            title = first_line.lstrip("# ").strip()[:200] or url
        if not summary:
            summary = extracted[:400].replace("\n", " ").strip()
        return title, summary

    def _handle_capture(self, args: dict) -> str:
        domain = args["domain"]
        type_ = args["type"]
        url = args.get("url")
        content = args.get("content")
        title = args.get("title")
        summary = args.get("summary")
        source = args.get("source") or ("manual" if not url else self._extractor)

        if url and not content:
            extracted = jina.extract(url) if self._extractor == "jina" else None
            if extracted:
                content = extracted
                title, summary = self._derive_title_summary(extracted, url, title, summary)
            else:
                content = url

        if not content and not url:
            return tool_error("aeon_capture requires content or url")

        dedup_key = "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] if url else None
        emb, model = embed_text(
            (title or "") + "\n" + (summary or content or ""),
            provider=self._embed_provider,
        )
        mid = q.capture_memory(
            self._db,
            type=type_, domain=domain, title=title, summary=summary, content=content,
            url=url, tags=args.get("tags") or [], project_id=args.get("project_id"),
            source=source,
            event_start=args.get("event_start_ms"), event_end=args.get("event_end_ms"),
            dedup_key=dedup_key, embedding=emb, embedding_model=model,
        )
        return tool_result(id=mid, status="captured", embedded=emb is not None)

    def _handle_search(self, args: dict) -> str:
        query = args["query"]
        emb, _ = embed_text(query, provider=self._embed_provider)
        hits = q.search_memories(
            self._db, query=query, embedding=emb,
            domain=args.get("domain"), type=args.get("type"),
            project_id=args.get("project_id"),
            limit=int(args.get("limit", 10)),
        )
        # Touch the recall counters from here so search_memories stays a pure read.
        if hits:
            q.touch_memories(self._db, [h.id for h in hits])
        return tool_result(results=[h.to_dict() for h in hits], count=len(hits))

    def _handle_calendar(self, args: dict) -> str:
        events = q.get_calendar(
            self._db, start_ms=int(args["start_ms"]), end_ms=int(args["end_ms"]),
            domain=args.get("domain"), limit=int(args.get("limit", 50)),
        )
        return tool_result(events=[e.to_dict() for e in events], count=len(events))

    def _handle_update(self, args: dict) -> str:
        emb, model = embed_text(args["content"], provider=self._embed_provider)
        rev = q.update_memory_content(
            self._db,
            memory_id=args["memory_id"],
            content=args["content"],
            summary=args.get("summary"),
            source=args.get("source"),
            embedding=emb, embedding_model=model,
        )
        return tool_result(memory_id=args["memory_id"], revision=rev)

    def _handle_set_tone(self, args: dict) -> str:
        sid = self._session_id
        current = self._tone.get(sid)
        weight = float(args.get("weight", 0.7))
        if args.get("preset"):
            new_state = apply_preset(current, args["preset"], weight)
        elif args.get("axes"):
            new_state = apply_axes(current, args["axes"], weight)
        else:
            return tool_error("aeon_set_tone requires preset or axes")
        self._tone.set(sid, new_state)
        return tool_result(tone=new_state.to_dict())

    def _handle_get_tone(self, args: dict) -> str:
        return tool_result(tone=self._tone.get(self._session_id).to_dict())

    # -- Auto-extract -------------------------------------------------------

    _URL_RE = re.compile(r'https?://[^\s<>"\']+')
    _DECISION_RE = re.compile(r'\b(?:we|i)\s+(?:decided|chose|went with|picked|will use)\s+(.+)', re.IGNORECASE)
    _TASK_RE = re.compile(r'\b(?:i need to|need to|todo:|todo |i should|i must)\s+(.+)', re.IGNORECASE)
    _AUTO_EXTRACT_URL_CAP = 10  # bound session-end network work

    def _auto_extract_from_transcript(self, messages: list) -> None:
        captured = 0
        urls_seen: set[str] = set()

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < 10:
                continue

            for url in self._URL_RE.findall(content):
                if url in urls_seen:
                    continue
                urls_seen.add(url)
                if len(urls_seen) > self._AUTO_EXTRACT_URL_CAP:
                    break
                try:
                    self._handle_capture({"domain": "inbox", "type": "link", "url": url,
                                          "source": "session-extract"})
                    captured += 1
                except Exception:
                    pass

            m = self._DECISION_RE.search(content)
            if m:
                try:
                    self._handle_capture({"domain": "work", "type": "note",
                                          "content": content[:600],
                                          "title": m.group(1)[:120].strip(),
                                          "source": "session-extract"})
                    captured += 1
                except Exception:
                    pass

            m = self._TASK_RE.search(content)
            if m:
                try:
                    self._handle_capture({"domain": "inbox", "type": "task",
                                          "content": content[:600],
                                          "title": m.group(1)[:120].strip(),
                                          "source": "session-extract"})
                    captured += 1
                except Exception:
                    pass

        if captured:
            logger.info("aeon auto-extract: captured %d items", captured)
