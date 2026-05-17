"""Shared helpers for ingest functions.

These run inside the provider via tool dispatch (db passed in), and can also
be invoked from cron via `hermes cron "Run aeon_pull_X" --skill aeon-memory`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)


# LLM helper — defaults to Groq (fast, free tier, llama-3.3-70b-versatile).
def llm_chat(prompt: str, *, system: Optional[str] = None,
             json_mode: bool = False, max_tokens: int = 1024,
             temperature: float = 0.2) -> str:
    import httpx
    provider = os.environ.get("CRON_LLM_PROVIDER", "groq").lower()

    if provider == "groq":
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set")
        url = "https://api.groq.com/openai/v1/chat/completions"
        model = os.environ.get("CRON_LLM_MODEL", "llama-3.3-70b-versatile")
    elif provider == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        url = "https://openrouter.ai/api/v1/chat/completions"
        model = os.environ.get("CRON_LLM_MODEL", "google/gemini-2.5-flash")
    elif provider == "gemini":
        return _gemini_chat(prompt, system=system, json_mode=json_mode,
                            max_tokens=max_tokens, temperature=temperature)
    else:
        raise RuntimeError(f"unknown CRON_LLM_PROVIDER: {provider}")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _gemini_chat(prompt: str, *, system: Optional[str], json_mode: bool,
                 max_tokens: int, temperature: float) -> str:
    import httpx
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = os.environ.get("CRON_LLM_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    resp = httpx.post(url, json=payload, timeout=60.0)
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def llm_json(prompt: str, **kwargs) -> Any:
    text = llm_chat(prompt, json_mode=True, **kwargs)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


# Profile-memory helpers
PROFILE_DEDUP_KEY = "profile:interests"


def get_profile_text(db) -> Optional[str]:
    row = db.execute(
        "SELECT content FROM memory_items WHERE dedup_key = ? LIMIT 1",
        (PROFILE_DEDUP_KEY,),
    ).fetchone()
    return row[0] if row else None


def upsert_profile(db, content: str, summary: Optional[str] = None) -> str:
    from store import queries as q
    from store.embed import embed_text
    existing = db.execute(
        "SELECT id FROM memory_items WHERE dedup_key = ? LIMIT 1",
        (PROFILE_DEDUP_KEY,),
    ).fetchone()
    emb, model = embed_text(content, provider=os.environ.get("HERMES_AEON_EMBED", "gemini"))
    if existing:
        mid = existing[0]
        q.update_memory_content(db, memory_id=mid, content=content,
                                summary=summary, source="cron:derive_profile",
                                embedding=emb, embedding_model=model)
    else:
        mid = q.capture_memory(
            db, type="note", domain="learning",
            title="Interest profile (derived from X bookmarks)",
            summary=summary, content=content,
            source="cron:derive_profile",
            dedup_key=PROFILE_DEDUP_KEY,
            embedding=emb, embedding_model=model,
        )
    return mid
