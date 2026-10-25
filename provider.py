"""Capture-only memory provider: each ended Hermes session is saved to Aeon Emerald as a transcript.

Recall is not done here. Hermes reaches Emerald's tools through its ``aeon`` MCP server entry; this
provider exists because only memory providers receive the full transcript at a real session end
(the plugin ``on_session_end`` hook fires per turn, and ``on_session_finalize`` gets only an id).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home

from .client import Emerald, EmeraldError, setting
from .session import Spool, send, session_captures

logger = logging.getLogger(__name__)

KEY_SETTING = "AEON_EMERALD_KEY"
PROMPT = (
    "Long-term memory lives in Aeon, reached through the `aeon` MCP tools and shared with the "
    "owner's other assistants. Search it before answering about the owner's past work, decisions, "
    "people or plans; use `context` for a briefing on a task, and `capture` when the owner asks you "
    "to remember something. Finished conversations are saved to Aeon automatically, so never capture "
    "the conversation itself."
)


class EmeraldSessionProvider(MemoryProvider):
    def __init__(self) -> None:
        self._home: Path = Path()
        self._session_id = ""
        self._platform = ""
        self._writes = False

    @property
    def name(self) -> str:
        return "aeon-emerald"

    def is_available(self) -> bool:
        try:
            setting("AEON_EMERALD_URL", get_hermes_home())
            setting(KEY_SETTING, get_hermes_home())
        except EmeraldError:
            return False
        return True

    def unavailable_reason(self) -> str:
        return f"set AEON_EMERALD_URL and {KEY_SETTING} in the profile's .env"

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._home = Path(kwargs.get("hermes_home") or get_hermes_home())
        self._session_id = session_id
        self._platform = str(kwargs.get("platform") or "")
        # Subagents, cron runs and flushes are not conversations with the owner.
        self._writes = kwargs.get("agent_context", "primary") == "primary"

    def system_prompt_block(self) -> str:
        return PROMPT

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        self._session_id = new_session_id

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._writes:
            return
        captures = session_captures(self._session_id, messages or [], platform=self._platform or None)
        spool = Spool(self._home / "aeon-emerald" / "spool")
        if not captures and not spool.pending():
            return
        try:
            client = Emerald.from_settings(self._home, KEY_SETTING)
        except EmeraldError as error:
            for arguments in captures:
                spool.put(arguments)
            logger.error("aeon-emerald: session %s not captured, %d part(s) spooled: %s",
                         self._session_id, len(captures), error)
            return
        try:
            failed = send(client, spool, captures)
        finally:
            client.close()
        if not failed:
            logger.info("aeon-emerald: captured session %s (%d part(s))", self._session_id, len(captures))
