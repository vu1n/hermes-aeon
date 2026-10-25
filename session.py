"""Session transcripts -> Emerald captures, through a disk spool that every capture passes.

Kept free of Hermes imports so it is testable without the host runtime.
"""
from __future__ import annotations

import hashlib
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
# Hermes marks the summary it inserts when it compacts a long context (agent/context_compressor.py) with
# this key, and starts its text with one of these prefixes (current and historical).
COMPACTION_FLAG = "_compressed_summary"
COMPACTION_PREFIXES = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")


def _text(content: Any) -> str:
    if isinstance(content, list):  # OpenAI-style content parts; images and other parts are dropped
        content = "\n".join(p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text" and p.get("text"))
    # PostgreSQL text cannot hold NUL (pasted binary or terminal output); Emerald would fail the capture.
    return content.replace("\x00", "").strip() if isinstance(content, str) else ""


def _compaction_summary(message: dict[str, Any]) -> Optional[str]:
    text = _text(message.get("content"))
    return text if message.get(COMPACTION_FLAG) or text.startswith(COMPACTION_PREFIXES) else None


def transcript_turns(messages: Iterable[dict[str, Any]]) -> list[str]:
    """What the user and Hermes said, in order. Left out: system messages, tool calls and results (bulky,
    and Emerald's extraction should work from what was said), and compaction summaries, which Hermes
    writes and inserts as a user message although the user never said them."""
    turns = []
    for message in messages:
        speaker = _SPEAKERS.get(message.get("role", ""))
        text = _text(message.get("content"))
        if speaker and text and _compaction_summary(message) is None:
            turns.append(f"{speaker}: {text}")
    return turns


def transcript_parts(turns: list[str], limit: int = PART_LIMIT) -> list[str]:
    """The turns split at turn boundaries into parts of at most ``limit`` characters."""
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


def session_captures(session_id: str, messages: Iterable[dict[str, Any]], *, ended_at: str,
                     platform: Optional[str] = None) -> list[dict[str, Any]]:
    """Capture arguments for one session's transcript at ``ended_at``; none if the user said nothing.

    A source is one compaction segment of a session, named by the latest compaction summary in the
    transcript. Hermes compacts in place: it ends the session once with the full turns, then later again
    under the same id with the summary and the turns since. Without the segment, that later transcript
    would be stored as a newer revision of the full one and outrank it. A session that only grows keeps
    its segment, so its newer transcript does replace the older one. Parts are numbered without a total,
    so growth re-sends only the last part."""
    messages = list(messages)
    turns = transcript_turns(messages)
    if not any(turn.startswith("User: ") for turn in turns):
        return []
    summaries = [summary for message in messages if (summary := _compaction_summary(message)) is not None]
    segment = hashlib.sha256(summaries[-1].encode("utf-8")).hexdigest()[:12] if summaries else "0"
    header = f"Hermes session {session_id}" + (f" ({platform})" if platform else "")
    return [capture_arguments(content=f"{header}, part {index}\n\n{part}", kind="transcript",
                              external_id=f"hermes-session:{session_id}:{segment}:{index}",
                              thread_id=session_id, occurred_at=ended_at)
            for index, part in enumerate(transcript_parts(turns), start=1)]


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
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(arguments, handle)
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
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

    Only a refusal of the capture's own content (or a file that cannot be read) moves it to ``rejected/``.
    Any other failure leaves it to be resent at a later session end: a 401 after a key rotation, or an
    Emerald database blip, must not sweep the backlog aside. No reply at all ends the drain, since the
    rest would each wait out the same timeout; any other failure moves on to the next capture, so one
    capture Emerald keeps failing does not hold back the rest."""
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
            if error.unreachable:
                logger.error("aeon-emerald: Emerald unreachable, captures wait in the spool: %s", error)
                break
            if error.input_refused:
                logger.error("aeon-emerald: capture of %s refused, moved to %s: %s",
                             external_id, spool.reject(path), error)
            else:
                logger.error("aeon-emerald: capture of %s failed, left in the spool: %s", external_id, error)
            continue
        path.unlink(missing_ok=True)
    return len(spool.pending())
