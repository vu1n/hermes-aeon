"""pre_llm_call hook — injects the tone block into the user message context.

Self-contained on purpose: hermes loads the plugin under one package name
for memory-provider discovery and another for the general PluginManager,
so the hook cannot share a Python singleton with the provider. State lives
on disk (``ToneStore`` JSON file) instead, and both the provider's
set_tone tool and this hook read/write the same file.

Returns ``{"context": <tone block>}`` so the plugin manager prepends it to
the user message (never the system prompt — preserves the prompt-cache
prefix).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from .codec import derive_from_message, tone_to_block
from .state import ToneStore

logger = logging.getLogger(__name__)


def _tone_path() -> Path:
    home = os.getenv("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "hermes-aeon" / "tone.json"


def on_pre_llm_call(
    *,
    session_id: str = "",
    user_message: Any = None,
    **_: Any,
) -> Optional[dict]:
    try:
        store = ToneStore(_tone_path())
    except Exception as e:
        logger.debug("aeon hook: ToneStore load failed: %s", e)
        return None

    sid = session_id or "default"
    current = store.get(sid)

    msg_text = ""
    if isinstance(user_message, str):
        msg_text = user_message
    elif isinstance(user_message, dict):
        msg_text = str(user_message.get("content", "") or "")

    derived = derive_from_message(msg_text, current) if msg_text else None
    if derived is not None:
        try:
            store.set(sid, derived)
        except Exception:
            pass
        current = derived

    return {"context": tone_to_block(current)}
