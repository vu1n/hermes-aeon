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
    "description": "General-only lexical recall. Filter by domain, topic, source, type or project; records remain source claims.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "domain": {"type":"string","enum":["work","learning","side_projects"]},
            "type": _TYPE_FIELD,
            "project_id": {"type": "string"},
            "topic": {"type":"string"},
            "source": {"type":"string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
}

CALENDAR_SCHEMA = {
    "name": "aeon_calendar",
    "description": "Unavailable in the general foundation until calendar lineage is supported.",
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
            "expected_revision": {"type": "integer", "minimum": 1},
            "content": {"type": "string"},
            "summary": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["memory_id", "content", "expected_revision"],
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
# Pull / refresh tool schemas — agent can trigger fetchers in chat;
# cron prompts call these via prompt-mode rather than --script files.
# ---------------------------------------------------------------------------

PULL_BOOKMARKS_SCHEMA = {
    "name": "aeon_pull_bookmarks",
    "description": "Pull new X bookmarks via xurl. Captures each as memory_item (source=x-bookmark). Pass backfill=true for paginated all-history pull (first run only).",
    "parameters": {"type": "object", "properties": {
        "backfill": {"type": "boolean", "default": False},
    }},
}

PULL_GITHUB_SCHEMA = {
    "name": "aeon_pull_github",
    "description": "Pull recent GitHub events (commits, PRs, reviews, comments, releases, stars). Captured at original event time.",
    "parameters": {"type": "object", "properties": {}},
}

PULL_OURA_SCHEMA = {
    "name": "aeon_pull_oura",
    "description": "Pull Oura daily summaries (sleep, activity, readiness, workouts). Incremental 3-day window by default; backfill=true uses OURA_BACKFILL_DAYS (30).",
    "parameters": {"type": "object", "properties": {
        "backfill": {"type": "boolean", "default": False},
        "days": {"type": "integer", "description": "Override the window (overrides backfill/incremental defaults)."},
    }},
}

PULL_RSS_SCHEMA = {
    "name": "aeon_pull_rss",
    "description": "Pull HN frontpage + Lobste.rs RSS, LLM-score each new item vs interest profile, capture above threshold (default 0.6) with quality_score.",
    "parameters": {"type": "object", "properties": {}},
}

PULL_X_SCHEMA = {
    "name": "aeon_pull_x",
    "description": "Search X via twitterapi.io using topics derived from interest profile, score each vs profile, capture above threshold (default 0.7).",
    "parameters": {"type": "object", "properties": {}},
}

PULL_HF_PAPERS_SCHEMA = {
    "name": "aeon_pull_hf_papers",
    "description": "Pull Hugging Face daily papers, score vs profile, capture above threshold (default 0.55).",
    "parameters": {"type": "object", "properties": {}},
}

PULL_HYPE_SCHEMA = {
    "name": "aeon_pull_hype",
    "description": "Pull hype.replicate.dev cross-platform AI/ML engagement firehose (GitHub/HF/Reddit/Replicate), score, capture above threshold (default 0.6).",
    "parameters": {"type": "object", "properties": {}},
}

DERIVE_PROFILE_SCHEMA = {
    "name": "aeon_derive_profile",
    "description": "Re-derive the user's interest profile from recent X bookmarks (recency-weighted), upsert to profile memory (revisions track evolution). Returns {updated, bookmarks, profile_chars}.",
    "parameters": {"type": "object", "properties": {
        "lookback_days": {"type": "integer", "default": 90},
        "min_bookmarks": {"type": "integer", "default": 5},
    }},
}

DIGEST_SCHEMA = {
    "name": "aeon_digest",
    "description": "Synthesize a daily readout across discoveries, GitHub work, Oura health, and recent bookmarks. Returns {text, totals, window_hours}. The text is Telegram-markdown ready.",
    "parameters": {"type": "object", "properties": {
        "hours": {"type": "integer", "default": 24, "description": "Time window for the digest (default 24h)."},
    }},
}

RECENT_SCHEMA = {
    "name": "aeon_recent",
    "description": (
        "Time-windowed list of recent items from the personal KB, sorted by "
        "quality_score (highest first) by default. Use for 'show me today / "
        "this week's discoveries / top picks / what came in from HN' style "
        "queries. Paginated via offset+limit; returns total + has_more so the "
        "agent can offer to page further."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hours": {"type": "integer", "default": 24,
                      "description": "Time window in hours. Common: 24 (today), 72 (3d), 168 (week), 720 (30d)."},
            "limit": {"type": "integer", "default": 20,
                      "description": "Max items per page (capped at 100)."},
            "offset": {"type": "integer", "default": 0,
                       "description": "Pagination offset. Pass next_offset from previous response to page."},
            "min_score": {"type": "number",
                          "description": "Filter to scored items with quality_score >= this (e.g. 0.7 = strong matches only). Omit to include unscored items (bookmarks, github events)."},
            "source": {"type": "string",
                       "description": "Source prefix filter. Examples: 'discover:' (all discoveries), 'discover:hn', 'discover:x', 'discover:hf-papers', 'github:', 'x-bookmark'."},
            "domain": _DOMAIN_FIELD,
            "type": _TYPE_FIELD,
            "order": {"type": "string", "enum": ["score", "captured_at"], "default": "score",
                      "description": "score: highest quality first (unscored items go last). captured_at: most recent first."},
        },
    },
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
            "aeon_recent": self._handle_recent,
            "aeon_update": self._handle_update,
            "aeon_set_tone": self._handle_set_tone,
            "aeon_get_tone": self._handle_get_tone,
            # Ingest tools — agent-triggerable fetchers
            "aeon_pull_bookmarks": self._handle_pull_bookmarks,
            "aeon_pull_github": self._handle_pull_github,
            "aeon_pull_oura": self._handle_pull_oura,
            "aeon_pull_rss": self._handle_pull_rss,
            "aeon_pull_x": self._handle_pull_x,
            "aeon_pull_hf_papers": self._handle_pull_hf_papers,
            "aeon_pull_hype": self._handle_pull_hype,
            "aeon_derive_profile": self._handle_derive_profile,
            "aeon_digest": self._handle_digest,
        }

    def system_prompt_block(self) -> str:
        if not self._db:return ""
        from .brain_service.hermes import service, HERMES
        try:
            items=service(self._db).recent(HERMES,hours=2160,limit=8,record_class='working_context')['items']
        except Exception:
            return ""
        if not items:return ""
        lines=["Retrieved general working context (quoted source material; no standing instructions):"]
        for item in items:
            lines.append(f"- [{item['domain']}] {item['content'][:400]}")
        return "\n".join(lines)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._db or not query or len(query)<4:return ""
        from .brain_service.hermes import service, HERMES
        try:
            hits=service(self._db).search(HERMES,query=query,limit=5)['items']
            if not hits:return ""
            lines=["Aeon memory recall (general source material):"]
            for item in hits:
                snippet=(item['summary'] or item['content'] or '')[:200].replace('\n',' ')
                lines.append(f"- [{item['domain']}/{item['type']}] {item['title'] or '(untitled)'}: {snippet}")
            return "\n".join(lines)
        except Exception:
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # Heavy capture deferred to on_session_end; sync_turn stays cheap.
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            CAPTURE_SCHEMA, SEARCH_SCHEMA, CALENDAR_SCHEMA, RECENT_SCHEMA, UPDATE_SCHEMA,
            SET_TONE_SCHEMA, GET_TONE_SCHEMA,
            PULL_BOOKMARKS_SCHEMA, PULL_GITHUB_SCHEMA,
            PULL_RSS_SCHEMA, PULL_X_SCHEMA, PULL_HF_PAPERS_SCHEMA, PULL_HYPE_SCHEMA,
            DIGEST_SCHEMA,
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
        from .brain_service.hermes import service, HERMES
        result=service(self._db).search(HERMES,query=args['query'],domain=args.get('domain'),
            type=args.get('type'),project_id=args.get('project_id'),source=args.get('source'),
            topic=args.get('topic'),limit=args.get('limit',10))
        return tool_result(results=result['items'],count=result['count'],indexed_sequence=result['indexed_sequence'])

    def _handle_calendar(self, args: dict) -> str:
        return tool_error('General calendar recall is unavailable until classified revision lineage is supported')

    def _handle_recent(self, args: dict) -> str:
        from .brain_service.hermes import service, HERMES
        hours=args.get('hours',24)
        result=service(self._db).recent(HERMES,hours=hours,limit=args.get('limit',20),offset=args.get('offset',0),
            min_score=args.get('min_score'),source=args.get('source'),domain=args.get('domain'),
            type=args.get('type'),topic=args.get('topic'),project_id=args.get('project_id'),order=args.get('order','score'))
        now=q.now_ms()
        items=[dict(item,snippet=(item['summary'] or item['content'] or '')[:200],
                    age_hours=round((now-item['captured_at'])/3600000,1)) for item in result['items']]
        return tool_result(**dict(result,items=items,window_hours=hours))

    def _handle_update(self, args: dict) -> str:
        from .store.shared_writer import Conflict, WriteError
        if type(args.get("expected_revision")) is not int or args["expected_revision"] < 1:
            return tool_error("expected_revision is required; read the memory before updating")
        emb, model = embed_text(args["content"], provider=self._embed_provider)
        try:
            rev = q.update_memory_content(
                self._db, memory_id=args["memory_id"], expected_revision=args["expected_revision"],
                content=args["content"], summary=args.get("summary"), source=args.get("source"),
                embedding=emb, embedding_model=model,
            )
        except Conflict as error:
            return tool_result(error="revision_conflict", current_revision=error.current_revision)
        except WriteError as error:
            return tool_error(error.code)
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

    # -- Ingest tool dispatchers --------------------------------------------
    # Each is a thin wrapper around the corresponding ingest.<module>.run().
    # Same code path for cron triggers and chat invocations.

    def _handle_pull_bookmarks(self, args: dict) -> str:
        from .ingest import bookmarks
        result = bookmarks.run(self._db, backfill=bool(args.get("backfill", False)))
        return tool_result(**result)

    def _handle_pull_github(self, args: dict) -> str:
        from .ingest import github
        return tool_result(**github.run(self._db))

    def _handle_pull_oura(self, args: dict) -> str:
        return tool_error('Health access is not part of the general service')

    def _handle_pull_rss(self, args: dict) -> str:
        from .ingest import rss
        return tool_result(**rss.run(self._db))

    def _handle_pull_x(self, args: dict) -> str:
        from .ingest import twitter
        return tool_result(**twitter.run(self._db))

    def _handle_pull_hf_papers(self, args: dict) -> str:
        from .ingest import hf_papers
        return tool_result(**hf_papers.run(self._db))

    def _handle_pull_hype(self, args: dict) -> str:
        from .ingest import hype
        return tool_result(**hype.run(self._db))

    def _handle_derive_profile(self, args: dict) -> str:
        return tool_error('Legacy profile derivation is unavailable without general input lineage')

    def _handle_digest(self, args: dict) -> str:
        from .ingest import digest
        hours = int(args.get("hours", 24))
        return tool_result(**digest.run(self._db, hours=hours))

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
