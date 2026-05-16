"""Per-session tone state persistence.

Backed by a JSON file under ``HERMES_HOME/hermes-aeon/tone.json``. The
provider and the ``pre_llm_call`` hook live in different ``sys.modules``
instances (hermes loads the plugin twice under different namespaces), so
disk is the only shared surface. ``get()`` reloads on every call so
writes from either side are immediately visible.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from .codec import DEFAULT_TONE, ToneState

logger = logging.getLogger(__name__)


class ToneStore:
    def __init__(self, path: Path):
        self._path = path
        self._cache: Dict[str, ToneState] = {}

    def _load(self) -> None:
        if not self._path.exists():
            self._cache = {}
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
        self._load()
        return self._cache.get(session_id, DEFAULT_TONE)

    def set(self, session_id: str, state: ToneState) -> None:
        self._load()  # merge with any concurrent writes from the other module instance
        self._cache[session_id] = state.clamp()
        self._save()
