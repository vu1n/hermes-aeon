"""Shared helpers for hermes-aeon cron scripts.

Scripts run via `hermes cron --script --no-agent` so they're pure background
workers. They share state with hermes-aeon (same libSQL db) by importing the
plugin's query layer directly.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Bootstrap import paths so scripts work both standalone and from hermes cron.
PLUGIN_ROOT = Path("/opt/hermes-aeon")
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

HERMES_AGENT_ROOT = Path("/usr/local/lib/hermes-agent")
if HERMES_AGENT_ROOT.exists() and str(HERMES_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_ROOT))


# Load ~/.hermes/.env once at import — cron jobs don't always inherit env.
def _load_env() -> None:
    env_file = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env()


# Logging — stderr so stdout stays clean for `hermes cron --deliver` payloads.
def setup_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                               datefmt="%Y-%m-%dT%H:%M:%SZ"))
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


# DB connection — single shared aeon.db file.
def get_db():
    from store.db import AeonDB
    db = AeonDB(
        db_path=os.environ.get("HERMES_AEON_DB", str(Path.home() / ".hermes" / "aeon.db")),
        turso_url=os.environ.get("HERMES_AEON_TURSO_URL"),
        turso_token=os.environ.get("HERMES_AEON_TURSO_TOKEN"),
    )
    db.connect()
    db.bootstrap_schema()
    return db


# LLM helper — defaults to Groq (fast, free tier, llama-3.3-70b-versatile).
# Override with CRON_LLM_PROVIDER=gemini|openrouter and CRON_LLM_MODEL.
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
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60.0,
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
    parts = [{"text": prompt}]
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
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
    """Same as llm_chat but parses JSON response. Strips markdown fences if present."""
    text = llm_chat(prompt, json_mode=True, **kwargs)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


# Profile-memory helpers — interest profile lives as a single memory_item.
PROFILE_DEDUP_KEY = "profile:interests"


def get_profile_text(db) -> Optional[str]:
    row = db.execute(
        "SELECT content FROM memory_items WHERE dedup_key = ? LIMIT 1",
        (PROFILE_DEDUP_KEY,),
    ).fetchone()
    return row[0] if row else None


def upsert_profile(db, content: str, summary: Optional[str] = None) -> str:
    """Insert or append-revision the interest profile memory."""
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
