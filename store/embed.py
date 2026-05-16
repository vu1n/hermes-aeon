"""Embedding helpers — Gemini text-embedding-004 by default, no-op when unavailable."""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GEMINI_MODEL = "models/text-embedding-004"
GEMINI_DIM = 768


def _gemini_embed(text: str) -> Optional[list[float]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/{GEMINI_MODEL}:embedContent?key={api_key}"
        resp = httpx.post(
            url,
            json={"model": GEMINI_MODEL, "content": {"parts": [{"text": text[:8000]}]}},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("embedding", {}).get("values")
    except Exception as e:
        logger.debug("gemini embed failed: %s", e)
        return None


def embed_text(text: str, *, provider: str = "gemini") -> tuple[Optional[list[float]], str]:
    """Return (embedding, model_name). embedding is None if provider unconfigured/failed."""
    if not text or provider == "none":
        return None, "none"
    if provider == "gemini":
        emb = _gemini_embed(text)
        return emb, GEMINI_MODEL if emb else "none"
    return None, "none"
