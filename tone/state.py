"""Per-session tone state persistence."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from .codec import DEFAULT_TONE, ToneState

logger = logging.getLogger(__name__)


class ToneStore:
    """Session-keyed tone state, persisted as JSON under HERMES_HOME/hermes-aeon/tone.json."""

    def __init__(self, path: Path):
        self._path = path
        self._cache: Dict[str, ToneState] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._cache = {sid: ToneState.from_dict(s) for sid, s in data.items()}
        except Exception as e:
            logger.warning("tone state load failed: %s", e)
            self._cache = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({sid: s.to_dict() for sid, s in self._cache.items()}, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("tone state save failed: %s", e)

    def get(self, session_id: str) -> ToneState:
        return self._cache.get(session_id, DEFAULT_TONE)

    def set(self, session_id: str, state: ToneState) -> None:
        self._cache[session_id] = state.clamp()
        self._save()
