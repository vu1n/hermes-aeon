"""r.jina.ai extractor — URL prefix yields markdown extraction. No API key."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def extract(url: str, *, timeout: float = 30.0) -> Optional[str]:
    """Return markdown extraction for *url* via r.jina.ai. None on failure."""
    if not url:
        return None
    target = url if url.startswith(("http://", "https://")) else "https://" + url
    try:
        resp = httpx.get(f"https://r.jina.ai/{target}", timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning("jina extract failed for %s: %s", url, e)
        return None
