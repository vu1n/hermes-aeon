"""Session transcripts -> Emerald captures, with a disk spool for captures that failed.

Kept free of Hermes imports so it is testable without the host runtime.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

from .client import Emerald, EmeraldError, capture_arguments

logger = logging.getLogger(__name__)

# Emerald's capture accepts up to 100,000 characters; leave room for the part header.
PART_LIMIT = 90_000
_SPEAKERS = {"user": "User", "assistant": "Hermes"}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):  # OpenAI-style content parts; images and other parts are dropped
        return "\n".join(p.get("text", "").strip() for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and p.get("text")).strip()
    return ""


def transcript_turns(messages: Iterable[dict[str, Any]]) -> list[str]:
    """User and assistant text, in order. Tool calls, tool results and system messages are left out:
    they are bulky, and Emerald's extraction should work from what was said."""
    turns = []
    for message in messages:
        speaker = _SPEAKERS.get(message.get("role", ""))
        text = _text(message.get("content"))
        if speaker and text:
            turns.append(f"{speaker}: {text}")
    return turns


def transcript_parts(messages: Iterable[dict[str, Any]], limit: int = PART_LIMIT) -> list[str]:
    """The transcript split at turn boundaries into captures of at most ``limit`` characters.
    Empty when the user said nothing, so sessions with no conversation are not stored."""
    turns = transcript_turns(messages)
    if not any(turn.startswith("User: ") for turn in turns):
        return []
    parts: list[str] = []
    current = ""
    for turn in turns:
        while len(turn) > limit:  # one oversized turn: hard-split it
            if current:
                parts.append(current)
                current = ""
            parts.append(turn[:limit])
            turn = turn[limit:]
        candidate = f"{current}\n\n{turn}" if current else turn
        if len(candidate) > limit:
            parts.append(current)
            candidate = turn
        current = candidate
    if current:
        parts.append(current)
    return parts


def session_captures(session_id: str, messages: Iterable[dict[str, Any]], *,
                     platform: Optional[str] = None) -> list[dict[str, Any]]:
    """Capture arguments for one ended session. A re-sent identical transcript replays its receipt."""
    parts = transcript_parts(messages)
    header = f"Hermes session {session_id}" + (f" ({platform})" if platform else "")
    captures = []
    for index, part in enumerate(parts, start=1):
        suffix = f":part{index}" if len(parts) > 1 else ""
        label = f"{header}, part {index} of {len(parts)}" if len(parts) > 1 else header
        captures.append(capture_arguments(
            content=f"{label}\n\n{part}", kind="transcript",
            external_id=f"hermes-session:{session_id}{suffix}", thread_id=session_id))
    return captures


class Spool:
    """Captures that failed, kept as JSON until a later session end sends them."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def put(self, arguments: dict[str, Any]) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._dir / f"{arguments['operation_id']}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(arguments, handle)
        return path

    def pending(self) -> list[Path]:
        return sorted(self._dir.glob("*.json")) if self._dir.is_dir() else []


def send(client: Emerald, spool: Spool, captures: list[dict[str, Any]]) -> int:
    """Send spooled captures, then ``captures``; spool whatever fails. Returns how many failed."""
    failed = 0
    for path in spool.pending():
        arguments = json.loads(path.read_text(encoding="utf-8"))
        try:
            client.call("capture", arguments)
        except EmeraldError as error:
            failed += 1
            logger.error("aeon-emerald: spooled capture %s still failing: %s", path.name, error)
            continue
        path.unlink()
    for arguments in captures:
        try:
            receipt = client.call("capture", arguments)
        except EmeraldError as error:
            failed += 1
            path = spool.put(arguments)
            logger.error("aeon-emerald: capture of %s failed, spooled to %s: %s",
                         arguments["source"]["external_id"], path, error)
            continue
        if receipt.get("status") != "stored":
            logger.warning("aeon-emerald: capture of %s returned %s (%s)",
                           arguments["source"]["external_id"], receipt.get("status"), receipt.get("reason"))
    return failed
