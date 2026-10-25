"""Session transcripts -> Emerald captures, through a disk spool that every capture passes.

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
    """Captures on their way to Emerald, one JSON file each, written before any send is tried.

    Several session ends may drain the same spool at once (gateway chats, the CLI): each file is sent
    and removed independently, and a re-send is harmless because Emerald replays the first receipt."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def put(self, arguments: dict[str, Any]) -> Path:
        """Write atomically (temp file, then rename), so a crash never leaves half a capture behind."""
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._dir / f"{arguments['operation_id']}.json"
        temp = self._dir / f".{path.name}.{os.getpid()}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(arguments, handle)
        os.replace(temp, path)
        return path

    def pending(self) -> list[Path]:
        """Waiting captures, oldest first, so a later revision of a session is stored after an earlier one."""
        if not self._dir.is_dir():
            return []
        entries = []
        for path in self._dir.glob("*.json"):
            try:
                entries.append((path.stat().st_mtime_ns, path.name, path))
            except FileNotFoundError:  # sent by another session end meanwhile
                continue
        return [path for _, _, path in sorted(entries)]

    def reject(self, path: Path) -> Optional[Path]:
        """Move a capture Emerald will never accept, or an unreadable file, out of the retry loop."""
        rejected = self._dir / "rejected"
        rejected.mkdir(exist_ok=True, mode=0o700)
        try:
            os.replace(path, rejected / path.name)
        except FileNotFoundError:
            return None
        return rejected / path.name


def drain(client: Emerald, spool: Spool) -> int:
    """Send spooled captures, oldest first, and return how many are still waiting.

    A transient failure ends the drain: the rest would fail the same way, each try costing a timeout,
    so they wait for the next session end. A capture Emerald rejects for good, or a file that cannot be
    read, moves to ``rejected/`` and is logged, rather than being retried at every session end."""
    for path in spool.pending():
        try:
            arguments = json.loads(path.read_text(encoding="utf-8"))
            external_id = arguments["source"]["external_id"]
        except FileNotFoundError:  # sent by another session end meanwhile
            continue
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.error("aeon-emerald: unreadable spooled capture %s moved to %s: %s",
                         path.name, spool.reject(path), error)
            continue
        try:
            client.capture(arguments)
        except EmeraldError as error:
            if error.transient:
                logger.error("aeon-emerald: Emerald unavailable, captures wait in the spool: %s", error)
                break
            logger.error("aeon-emerald: capture of %s rejected, moved to %s: %s",
                         external_id, spool.reject(path), error)
            continue
        path.unlink(missing_ok=True)
    return len(spool.pending())
