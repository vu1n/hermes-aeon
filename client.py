"""Aeon Emerald over MCP: one stateless JSON-RPC ``tools/call`` per request, bearer API key.

Shared by the memory provider (imported as ``<plugin>.client``) and the cron jobs in ``jobs/``
(imported as top-level ``client``), so this module must not use relative imports.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import httpx

# Fixed so operation ids, and with them Emerald's per-(principal, operation_id) replay, survive restarts.
# The string is an identifier, not a path: changing it would re-store everything already captured.
_OPERATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/vu1n/hermes-aeon/aeon_emerald")
SOURCE_KINDS = frozenset({"text", "transcript", "repository", "document", "tool"})


class EmeraldError(RuntimeError):
    """A failed Emerald call, with the tool name and the server's code or HTTP status in the message.

    ``unreachable``: no reply at all (connection failed or timed out), so the next call would fail the
    same way. ``input_refused``: Emerald refused this request's input itself (INVALID_INPUT, or a capture
    rejected for its sensitivity), so resending it cannot succeed. Neither: the endpoint, the credentials
    or Emerald failed (any other HTTP status, UNAUTHENTICATED, FORBIDDEN, UNAVAILABLE...), which says
    nothing about this input. ``code`` and ``detail`` are the tool's error code and message, if any."""

    def __init__(self, message: str, *, unreachable: bool = False, input_refused: bool = False,
                 code: Optional[str] = None, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.unreachable = unreachable
        self.input_refused = input_refused
        self.code = code
        self.detail = detail


class SettingMissing(RuntimeError):
    """A required setting is in neither the environment nor the profile's .env."""


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE pairs from a dotenv file; missing file -> {}."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def setting(name: str, hermes_home: Path) -> str:
    """``name`` from the process env, else from ``<hermes_home>/.env``.

    Hermes scrubs secrets from cron script environments, so jobs read the profile's .env directly."""
    value = os.environ.get(name) or read_env_file(hermes_home / ".env").get(name, "")
    if not value:
        raise SettingMissing(f"{name} is not set in the environment or {hermes_home / '.env'}")
    return value


def operation_id(*parts: str) -> str:
    return str(uuid.uuid5(_OPERATION_NAMESPACE, "\x1f".join(parts)))


def content_version(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


class Emerald:
    def __init__(self, url: str, key: str, *, timeout: float = 20.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self._url = url
        self._http = httpx.Client(
            timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"},
        )

    @classmethod
    def from_settings(cls, hermes_home: Path, key_name: str, **kwargs: Any) -> "Emerald":
        return cls(setting("AEON_EMERALD_URL", hermes_home), setting(key_name, hermes_home), **kwargs)

    def close(self) -> None:
        self._http.close()

    def call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The tool's structured result; raises EmeraldError on transport, protocol or tool errors."""
        request = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call",
                   "params": {"name": tool, "arguments": dict(arguments)}}
        try:
            response = self._http.post(self._url, json=request)
        except httpx.HTTPError as error:
            raise EmeraldError(f"{tool}: {type(error).__name__}: {error} ({self._url})", unreachable=True) from error
        if response.status_code != 200:
            raise EmeraldError(f"{tool}: HTTP {response.status_code}: {response.text[:300]}")
        try:
            body = _response_message(response, request["id"])
        except ValueError as error:
            raise EmeraldError(f"{tool}: unreadable response: {error}: {response.text[:300]}") from error
        if "error" in body:
            error = body["error"]
            raise EmeraldError(f"{tool}: JSON-RPC {error.get('code')}: {error.get('message')}")
        result = body.get("result") or {}
        structured = result.get("structuredContent")
        if result.get("isError"):
            detail = structured or [part.get("text") for part in result.get("content") or []]
            code = structured.get("code") if isinstance(structured, dict) else None
            message = structured.get("message") if isinstance(structured, dict) else None
            raise EmeraldError(f"{tool}: {detail}", input_refused=code == "INVALID_INPUT", code=code, detail=message)
        if not isinstance(structured, dict):
            raise EmeraldError(f"{tool}: result has no structuredContent: {json.dumps(result)[:300]}")
        return structured

    def capture(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Send what ``capture_arguments`` built; the receipt if Emerald stored the episode or already had it.

        Re-sending identical content replays the first receipt; changed content is a new version.
        Any other outcome, such as ``rejected_sensitive``, raises EmeraldError."""
        try:
            receipt = self.call("capture", arguments)
        except EmeraldError as error:
            # The operation id covers the external id and the content, so a reused id with other input
            # means Emerald already holds this content, sent earlier with other metadata (such as an
            # earlier occurred_at). Emerald's other CONFLICT (a database deadlock) is not that.
            if error.code == "CONFLICT" and "operation_id" in (error.detail or ""):
                return {"status": "duplicate", "reason": str(error)}
            raise
        if receipt.get("status") not in ("stored", "duplicate"):
            raise EmeraldError(f"capture: {receipt.get('status')}: {receipt.get('reason')}",
                               input_refused=receipt.get("status") == "rejected_sensitive")
        return receipt


def _response_message(response: httpx.Response, request_id: str) -> dict[str, Any]:
    """The JSON-RPC reply to ``request_id``. MCP servers answer with plain JSON or, when the client
    accepts it (the spec says clients must), a server-sent event stream; Emerald does the latter."""
    if not response.headers.get("content-type", "").startswith("text/event-stream"):
        return response.json()
    for event in response.text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if data:
            message = json.loads(data)
            if message.get("id") == request_id:
                return message
    raise ValueError("no reply to this request in the event stream")


def capture_arguments(*, content: str, kind: str, external_id: str, thread_id: Optional[str] = None,
                      occurred_at: Optional[str] = None, project: Optional[str] = None,
                      sensitivity: Iterable[str] = (), uri: Optional[str] = None) -> dict[str, Any]:
    if kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source kind {kind!r}")
    version = content_version(content)
    source: dict[str, Any] = {"kind": kind, "external_id": external_id, "version": version}
    if uri:
        source["uri"] = uri
    arguments: dict[str, Any] = {
        "operation_id": operation_id(external_id, version),
        "content": content,
        "source": source,
        "sensitivity": sorted(set(sensitivity)),
    }
    if thread_id:
        arguments["thread_id"] = thread_id
    if occurred_at:
        arguments["occurred_at"] = occurred_at
    if project:
        arguments["project"] = project
    return arguments
