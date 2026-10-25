"""aeon_emerald: Emerald client, session transcripts, provider, and the cron jobs, against fake HTTP."""
import importlib
import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT

client = importlib.import_module("aeon_emerald.client")
session = importlib.import_module("aeon_emerald.session")


@pytest.fixture
def provider(monkeypatch):
    """The provider module, with the Hermes host stubbed only for this test when it is not installed
    (a lasting stub would make the host-gated e2e tests run instead of skip)."""
    if importlib.util.find_spec("agent") is None:
        agent = types.ModuleType("agent")
        agent.memory_provider = types.ModuleType("agent.memory_provider")
        agent.memory_provider.MemoryProvider = type("MemoryProvider", (), {})
        monkeypatch.setitem(sys.modules, "agent", agent)
        monkeypatch.setitem(sys.modules, "agent.memory_provider", agent.memory_provider)
    if importlib.util.find_spec("hermes_constants") is None:
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path("/nonexistent-hermes-home")
        monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.delitem(sys.modules, "aeon_emerald.provider", raising=False)
    return importlib.import_module("aeon_emerald.provider")


def load_job(name):
    spec = importlib.util.spec_from_file_location(f"aeon_emerald_job_{name}", PACKAGE / "jobs" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event_stream(*messages):
    """A server-sent event reply, the way Emerald answers a client that accepts both formats."""
    text = "".join(f"event: message\ndata: {json.dumps(m)}\n\n" for m in messages)
    return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


class ToolFailure(Exception):
    """Raised by a fake handler to make the tool fail with Emerald's error ``code``."""

    def __init__(self, code: str, message: str = "failed") -> None:
        super().__init__(message)
        self.code = code


def mcp_server(handler):
    """A fake Emerald /mcp endpoint: ``handler(tool, arguments)`` returns structured content or raises
    (ToolFailure for a given error code, LookupError for NOT_FOUND)."""
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        params = body["params"]
        calls.append((params["name"], params["arguments"], request.headers["authorization"]))
        try:
            value = handler(params["name"], params["arguments"])
        except (LookupError, ToolFailure) as error:
            code = getattr(error, "code", "NOT_FOUND")
            return event_stream({"jsonrpc": "2.0", "id": body["id"], "result": {
                "isError": True, "content": [{"type": "text", "text": f"{code}: {error}"}],
                "structuredContent": {"code": code, "message": str(error)}}})
        return event_stream({"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}})

    return httpx.MockTransport(respond), calls


def stored(tool, arguments):
    return {"status": "stored", "episode_id": "e1", "reason": None}


# -- client -------------------------------------------------------------------------------------------

def test_capture_arguments_are_deterministic_per_content():
    a = client.capture_arguments(content="hello", kind="text", external_id="x:1")
    b = client.capture_arguments(content="hello", kind="text", external_id="x:1")
    c = client.capture_arguments(content="hello again", kind="text", external_id="x:1")
    assert a == b
    assert a["operation_id"] != c["operation_id"]
    assert a["source"]["version"] != c["source"]["version"]
    assert a["source"]["external_id"] == c["source"]["external_id"] == "x:1"


def test_capture_arguments_reject_unknown_source_kind():
    with pytest.raises(ValueError):
        client.capture_arguments(content="x", kind="feed", external_id="x")


def test_call_returns_structured_content_and_sends_bearer_key():
    transport, calls = mcp_server(lambda tool, args: {"principal": "p1"})
    emerald = client.Emerald("http://aeon/mcp", "secret", transport=transport)
    assert emerald.call("whoami", {}) == {"principal": "p1"}
    assert calls == [("whoami", {}, "Bearer secret")]


def test_call_raises_with_tool_name_on_tool_error():
    def missing(tool, args):
        raise LookupError("no such claim")
    transport, _ = mcp_server(missing)
    with pytest.raises(client.EmeraldError, match="fetch: .*NOT_FOUND") as raised:
        client.Emerald("http://aeon/mcp", "k", transport=transport).call("fetch", {"id": "x"})
    assert raised.value.code == "NOT_FOUND" and raised.value.detail == "no such claim"


def _connect_error(request):
    raise httpx.ConnectError("unreachable", request=request)


def _tool_failure(code, message="failed"):
    def reply(tool, args):
        raise ToolFailure(code, message)
    return mcp_server(reply)[0]


def _json_rpc_error(request):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"],
                                     "error": {"code": -32603, "message": "internal"}})


@pytest.mark.parametrize("transport, unreachable, input_refused", [
    (httpx.MockTransport(_connect_error), True, False),
    (_tool_failure("INVALID_INPUT"), False, True),
    (httpx.MockTransport(lambda r: httpx.Response(401, text="unauthorized")), False, False),
    (httpx.MockTransport(lambda r: httpx.Response(503, text="down")), False, False),
    (httpx.MockTransport(lambda r: httpx.Response(200, text="<html>Access</html>")), False, False),
    (httpx.MockTransport(_json_rpc_error), False, False),
    (_tool_failure("UNAUTHENTICATED"), False, False),
    (_tool_failure("FORBIDDEN"), False, False),
    (_tool_failure("UNAVAILABLE"), False, False),
], ids=["unreachable", "INVALID_INPUT", "401", "503", "html", "json-rpc", "UNAUTHENTICATED", "FORBIDDEN",
        "UNAVAILABLE"])
def test_errors_say_whether_emerald_answered_and_whether_it_refused_the_input(transport, unreachable, input_refused):
    with pytest.raises(client.EmeraldError) as raised:
        client.Emerald("http://aeon/mcp", "k", transport=transport).call("capture", {})
    assert (raised.value.unreachable, raised.value.input_refused) == (unreachable, input_refused)


def test_capture_counts_a_reused_operation_id_as_already_stored():
    # Same operation id (same external id and content), different input: Emerald holds the content.
    arguments = client.capture_arguments(content="x", kind="text", external_id="x:1")
    conflict = _tool_failure("CONFLICT", "operation_id was already used with different input")
    assert client.Emerald("http://aeon/mcp", "k", transport=conflict).capture(arguments)["status"] == "duplicate"


def test_capture_does_not_count_a_deadlock_as_stored():
    arguments = client.capture_arguments(content="x", kind="text", external_id="x:1")
    deadlock = _tool_failure("CONFLICT", "deadlock detected")
    with pytest.raises(client.EmeraldError, match="deadlock") as raised:
        client.Emerald("http://aeon/mcp", "k", transport=deadlock).capture(arguments)
    assert not raised.value.input_refused


@pytest.mark.parametrize("status", ["stored", "duplicate"])
def test_capture_returns_the_receipt_when_emerald_has_the_episode(status):
    transport, calls = mcp_server(lambda tool, args: {"status": status, "reason": None})
    arguments = client.capture_arguments(content="x", kind="text", external_id="x:1")
    assert client.Emerald("http://aeon/mcp", "k", transport=transport).capture(arguments)["status"] == status
    assert calls[0][:2] == ("capture", arguments)


def test_capture_raises_when_emerald_rejects_the_episode():
    transport, _ = mcp_server(lambda tool, args: {"status": "rejected_sensitive", "reason": "sensitivity not opted in"})
    emerald = client.Emerald("http://aeon/mcp", "k", transport=transport)
    with pytest.raises(client.EmeraldError, match="capture: rejected_sensitive: sensitivity not opted in") as raised:
        emerald.capture(client.capture_arguments(content="x", kind="text", external_id="x:1"))
    assert raised.value.input_refused


def test_call_reads_plain_json_replies():
    def reply(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"],
                                         "result": {"structuredContent": {"items": []}}})
    emerald = client.Emerald("http://aeon/mcp", "k", transport=httpx.MockTransport(reply))
    assert emerald.call("timeline", {}) == {"items": []}


def test_call_reads_crlf_event_streams_with_several_events():
    def reply(request):
        note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}
        answer = {"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": {"structuredContent": {"ok": 1}}}
        text = "".join(f"event: message\r\ndata: {json.dumps(m)}\r\n\r\n" for m in (note, answer))
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
    assert client.Emerald("http://aeon/mcp", "k", transport=httpx.MockTransport(reply)).call("whoami", {}) == {"ok": 1}


@pytest.mark.parametrize("response, pattern", [
    (httpx.Response(401, text="unauthorized"), "HTTP 401"),
    (httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}}),
     "JSON-RPC -32602"),
    (httpx.Response(200, text="<html>"), "unreadable response"),
    (event_stream({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}),
     "unreadable response: no reply"),
])
def test_call_raises_on_transport_and_protocol_errors(response, pattern):
    emerald = client.Emerald("http://aeon/mcp", "k", transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(client.EmeraldError, match=pattern):
        emerald.call("search", {"query": "q"})


def test_setting_prefers_environment_then_profile_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('# comment\nexport AEON_A="from-file"\nAEON_B=b\n')
    monkeypatch.setenv("AEON_B", "from-env")
    assert client.setting("AEON_A", tmp_path) == "from-file"
    assert client.setting("AEON_B", tmp_path) == "from-env"
    with pytest.raises(client.SettingMissing, match="AEON_C is not set"):
        client.setting("AEON_C", tmp_path)


# -- session transcripts --------------------------------------------------------------------------------

MESSAGES = [
    {"role": "system", "content": "You are Hermes."},
    {"role": "user", "content": "What did we decide about the VPS?"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
    {"role": "tool", "content": "{\"huge\": \"tool output\"}"},
    {"role": "assistant", "content": [{"type": "text", "text": "We moved Hermes to Emerald."}]},
]


ENDED = "2026-10-25T10:00:00+00:00"
SUMMARY = {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted. "
                                      "## Goal: move Hermes to Emerald", "_compressed_summary": True}


def captures_of(session_id, messages=MESSAGES, **kwargs):
    return session.session_captures(session_id, messages, ended_at=ENDED, **kwargs)


def test_transcript_keeps_only_what_the_user_and_hermes_said():
    assert session.transcript_turns(MESSAGES) == [
        "User: What did we decide about the VPS?", "Hermes: We moved Hermes to Emerald."]


def test_transcripts_drop_nul_characters_postgresql_cannot_store():
    pasted = [{"role": "user", "content": "binary\x00paste"},
              {"role": "assistant", "content": [{"type": "text", "text": "seen\x00"}]}]
    assert session.transcript_turns(pasted) == ["User: binarypaste", "Hermes: seen"]


@pytest.mark.parametrize("summary", [
    SUMMARY,
    {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted."},
    {"role": "assistant", "content": "[CONTEXT SUMMARY]: the user moved Hermes to Emerald"},
    {"role": "user", "content": "anything", "_compressed_summary": True},
], ids=["flag-and-prefix", "prefix", "legacy-prefix", "flag"])
def test_compaction_summaries_are_not_the_users_words(summary):
    assert session.transcript_turns([summary, *MESSAGES[1:]]) == session.transcript_turns(MESSAGES)
    assert captures_of("s1", [summary, {"role": "assistant", "content": "ok"}]) == []


def test_sessions_without_user_text_are_not_captured():
    assert captures_of("s1", [{"role": "assistant", "content": "Good morning"}]) == []


def test_long_transcripts_split_at_turn_boundaries_and_oversized_turns_are_cut():
    turns = [f"User: {'a' * 40}", f"Hermes: {'b' * 40}", f"User: {'c' * 130}"]
    parts = session.transcript_parts(turns, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert parts[0] == f"User: {'a' * 40}\n\nHermes: {'b' * 40}"
    assert "".join(parts[1:]) == f"User: {'c' * 130}"


def test_turns_that_exactly_fill_a_part_stay_in_one_part():
    turns = [f"User: {'a' * 10}", f"Hermes: {'b' * 10}"]
    whole = "\n\n".join(turns)
    assert session.transcript_parts(turns, limit=len(whole)) == [whole]


def test_session_captures_are_dated_and_threaded_by_session():
    (only,) = captures_of("s1", platform="telegram")
    assert only["source"]["kind"] == "transcript"
    assert only["source"]["external_id"] == "hermes-session:s1:0:1"
    assert only["thread_id"] == "s1" and only["occurred_at"] == ENDED
    assert only["content"].startswith("Hermes session s1 (telegram), part 1\n\nUser: ")


def test_a_growing_session_resends_only_its_last_part():
    fills_part_1 = {"role": "user", "content": "x" * (session.PART_LIMIT - len("User: "))}
    first = [fills_part_1, {"role": "user", "content": "short"}]
    later = [*first, {"role": "assistant", "content": "more"}]
    before, after = captures_of("s2", first), captures_of("s2", later)
    assert [c["source"]["external_id"] for c in after] == ["hermes-session:s2:0:1", "hermes-session:s2:0:2"]
    assert before[0] == after[0]  # unchanged part: same operation id, replayed
    assert before[1]["operation_id"] != after[1]["operation_id"]


def test_each_compaction_segment_is_its_own_source():
    full = captures_of("s1", MESSAGES)
    compacted = captures_of("s1", [MESSAGES[0], SUMMARY, {"role": "user", "content": "And the digest?"}])
    grown = captures_of("s1", [MESSAGES[0], SUMMARY, {"role": "user", "content": "And the digest?"},
                               {"role": "assistant", "content": "It reads FreshRSS."}])
    assert full[0]["source"]["external_id"] != compacted[0]["source"]["external_id"]
    assert compacted[0]["source"]["external_id"] == grown[0]["source"]["external_id"]


def test_the_latest_compaction_summary_names_the_segment():
    older = {**SUMMARY, "content": "[CONTEXT COMPACTION — REFERENCE ONLY] an older summary"}
    question = {"role": "user", "content": "And the digest?"}
    both = captures_of("s1", [older, {"role": "user", "content": "hi"}, SUMMARY, question])
    latest_only = captures_of("s1", [SUMMARY, question])
    assert both[0]["source"]["external_id"] == latest_only[0]["source"]["external_id"]


def emerald_with(transport):
    return client.Emerald("http://aeon/mcp", "k", transport=transport)


def spooled(spool, *session_ids):
    captures = [captures_of(sid)[0] for sid in session_ids]
    for arguments in captures:
        spool.put(arguments)
    return captures


def test_spooled_captures_wait_out_an_outage_and_are_sent_later(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    captures = spooled(spool, "s1")
    assert session.drain(emerald_with(httpx.MockTransport(_connect_error)), spool) == 1
    transport, calls = mcp_server(stored)
    assert session.drain(emerald_with(transport), spool) == 0
    assert [c[1] for c in calls] == captures


def test_an_outage_ends_the_drain_after_one_try(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    spooled(spool, "s1", "s2", "s3")
    tries = []

    def unreachable(request):
        tries.append(request)
        return _connect_error(request)
    assert session.drain(emerald_with(httpx.MockTransport(unreachable)), spool) == 3
    assert len(tries) == 1


@pytest.mark.parametrize("failure", [
    lambda request: httpx.Response(401, text="unauthorized"),  # a rotated key, or Emerald's key check failing
    lambda request: _tool_failure("FORBIDDEN").handle_request(request),
], ids=["401", "FORBIDDEN"])
def test_an_endpoint_or_credential_failure_keeps_every_capture(tmp_path, failure):
    spool = session.Spool(tmp_path / "spool")
    spooled(spool, "s1", "s2", "s3")
    tries = []

    def failing(request):
        tries.append(request)
        return failure(request)
    assert session.drain(emerald_with(httpx.MockTransport(failing)), spool) == 3
    assert len(tries) == 3 and not (tmp_path / "spool" / "rejected").exists()


def test_a_capture_emerald_keeps_failing_does_not_hold_back_the_rest(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    spooled(spool, "s1", "s2", "s3")

    def capture(tool, args):
        if args["thread_id"] == "s1":
            raise ToolFailure("UNAVAILABLE", "invalid byte sequence")
        return stored(tool, args)
    transport, calls = mcp_server(capture)
    assert session.drain(emerald_with(transport), spool) == 1
    assert sorted(c[1]["thread_id"] for c in calls) == ["s1", "s2", "s3"]
    assert [json.loads(p.read_text())["thread_id"] for p in spool.pending()] == ["s1"]


def test_the_spool_drains_oldest_first(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    captures = spooled(spool, "s1", "s2", "s3")
    for age, arguments in zip((3, 1, 2), captures):  # s1 oldest, then s3, then s2
        path = tmp_path / "spool" / f"{arguments['operation_id']}.json"
        os.utime(path, ns=(10**18 - age * 10**9, 10**18 - age * 10**9))
    transport, calls = mcp_server(stored)
    session.drain(emerald_with(transport), spool)
    assert [c[1]["thread_id"] for c in calls] == ["s1", "s3", "s2"]


def test_a_capture_emerald_rejects_leaves_the_retry_loop(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    spooled(spool, "s1")
    transport, _ = mcp_server(lambda tool, args: {"status": "rejected_sensitive", "reason": "not opted in"})
    assert session.drain(emerald_with(transport), spool) == 0
    assert len(list((tmp_path / "spool" / "rejected").glob("*.json"))) == 1


def test_an_unreadable_spool_file_is_set_aside_and_the_rest_still_sent(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    (tmp_path / "spool").mkdir()
    (tmp_path / "spool" / "half-written.json").write_text('{"operation_id": ')
    captures = spooled(spool, "s1")
    transport, calls = mcp_server(stored)
    assert session.drain(emerald_with(transport), spool) == 0
    assert [c[1] for c in calls] == captures
    assert (tmp_path / "spool" / "rejected" / "half-written.json").exists()


def test_a_capture_another_session_end_already_sent_is_skipped(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    first, second = spooled(spool, "s1", "s2")
    second_path = tmp_path / "spool" / f"{second['operation_id']}.json"

    class OtherDrainerWins:
        def capture(self, arguments):  # while s1 is sent, another session end sends and removes s2
            second_path.unlink()
            return {"status": "stored"}
    assert session.drain(OtherDrainerWins(), spool) == 0


def test_a_capture_another_drainer_sent_while_this_one_did_is_not_an_error(tmp_path, caplog):
    spool = session.Spool(tmp_path / "spool")
    (only,) = spooled(spool, "s1")

    class SentTwice:
        def capture(self, arguments):  # the other drainer sends the same file and removes it first
            (tmp_path / "spool" / f"{only['operation_id']}.json").unlink()
            return {"status": "stored"}
    assert session.drain(SentTwice(), spool) == 0
    assert "unreadable" not in caplog.text and not (tmp_path / "spool" / "rejected").exists()


def test_a_file_gone_before_it_is_read_is_skipped_quietly(tmp_path, caplog):
    spool = session.Spool(tmp_path / "spool")
    first, second = spooled(spool, "s1", "s2")
    second_path = tmp_path / "spool" / f"{second['operation_id']}.json"

    class OtherDrainerWins:
        def capture(self, arguments):
            second_path.unlink(missing_ok=True)
            return {"status": "stored"}
    assert session.drain(OtherDrainerWins(), spool) == 0
    assert "unreadable" not in caplog.text


def test_a_capture_appears_in_the_spool_only_once_fully_written(tmp_path, monkeypatch):
    # A kill mid-write must not leave half a capture where the drain would read it.
    spool = session.Spool(tmp_path / "spool")
    arguments = captures_of("s1")[0]
    final = tmp_path / "spool" / f"{arguments['operation_id']}.json"
    visible_mid_write, real_dump = [], session.json.dump

    def watching(value, handle):
        visible_mid_write.append(final.exists())
        real_dump(value, handle)
    monkeypatch.setattr(session.json, "dump", watching)
    spool.put(arguments)
    assert visible_mid_write == [False] and json.loads(final.read_text()) == arguments


def test_a_failed_spool_write_leaves_nothing_behind(tmp_path, monkeypatch):
    spool = session.Spool(tmp_path / "spool")

    def half_then_fail(value, handle):
        handle.write('{"operation_id": ')
        raise OSError("disk full")
    monkeypatch.setattr(session.json, "dump", half_then_fail)
    with pytest.raises(OSError, match="disk full"):
        spool.put(captures_of("s1")[0])
    assert spool.pending() == [] and list((tmp_path / "spool").iterdir()) == []


def test_spooled_transcripts_are_readable_only_by_their_owner(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    path = spool.put(captures_of("s1")[0])
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "spool").stat().st_mode & 0o777 == 0o700
    assert [p.name for p in (tmp_path / "spool").iterdir()] == [path.name]  # no temp file left behind


# -- provider ---------------------------------------------------------------------------------------------

@pytest.fixture
def drained(provider, monkeypatch):
    """Record, at each drain, the external ids waiting in the spool; the drain then sends nothing."""
    seen = []
    monkeypatch.setattr(provider.Emerald, "from_settings", classmethod(lambda cls, home, key: object.__new__(cls)))
    monkeypatch.setattr(provider.Emerald, "close", lambda self: None)

    def drain(emerald, spool):
        seen.append([json.loads(p.read_text())["source"]["external_id"] for p in spool.pending()])
        return len(spool.pending())
    monkeypatch.setattr(provider, "drain", drain)
    return seen


def test_provider_captures_primary_sessions_only(provider, drained, tmp_path):
    cron = provider.EmeraldSessionProvider()
    cron.initialize("s-cron", hermes_home=str(tmp_path), platform="cron", agent_context="cron")
    cron.on_session_end(MESSAGES)
    assert drained == []

    chat = provider.EmeraldSessionProvider()
    chat.initialize("s1", hermes_home=str(tmp_path), platform="telegram")
    chat.on_session_switch("s2")
    chat.on_session_end(MESSAGES)
    assert drained == [["hermes-session:s2:0:1"]]
    assert chat.get_tool_schemas() == []


def test_provider_spools_when_settings_are_missing(provider, tmp_path):
    chat = provider.EmeraldSessionProvider()
    chat.initialize("s1", hermes_home=str(tmp_path), platform="telegram")
    chat.on_session_end(MESSAGES)
    assert len(session.Spool(tmp_path / "aeon-emerald" / "spool").pending()) == 1


def test_provider_is_available_only_with_url_and_key(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "get_hermes_home", lambda: tmp_path)
    (tmp_path / ".env").write_text("AEON_EMERALD_URL=http://aeon/mcp\n")
    assert not provider.EmeraldSessionProvider().is_available()
    (tmp_path / ".env").write_text("AEON_EMERALD_URL=http://aeon/mcp\nAEON_EMERALD_KEY=k\n")
    assert provider.EmeraldSessionProvider().is_available()


def test_provider_resends_the_spool_at_a_session_end_with_nothing_new(provider, drained, tmp_path):
    session.Spool(tmp_path / "aeon-emerald" / "spool").put(captures_of("old")[0])
    chat = provider.EmeraldSessionProvider()
    chat.initialize("s1", hermes_home=str(tmp_path), platform="telegram")
    chat.on_session_end([{"role": "assistant", "content": "Good morning"}])
    assert drained == [["hermes-session:old:0:1"]]


def started(provider, tmp_path, **kwargs):
    chat = provider.EmeraldSessionProvider()
    chat.initialize("s1", hermes_home=str(tmp_path), platform="cli", **kwargs)
    chat.sync_turn("What did we decide?", "Emerald.", session_id="s1", messages=MESSAGES)
    return chat


@pytest.mark.parametrize("reason", ["resumed_other", "branched"])
def test_a_session_left_by_resume_or_branch_is_saved_on_the_switch(provider, drained, tmp_path, reason):
    started(provider, tmp_path).on_session_switch("s2", parent_session_id="s1", reset=False, reason=reason)
    assert drained == [["hermes-session:s1:0:1"]]


def test_a_departed_session_is_saved_once(provider, drained, tmp_path):
    chat = started(provider, tmp_path)
    chat.on_session_switch("s2", reset=False, reason="resumed_other")
    chat.on_session_switch("s3", reset=False, reason="branched")
    assert drained == [["hermes-session:s1:0:1"]]


def test_a_session_that_ended_is_not_saved_again_on_the_switch(provider, drained, tmp_path):
    chat = started(provider, tmp_path)
    chat.on_session_end(MESSAGES)
    chat.on_session_switch("s2", reset=True)
    assert drained == [["hermes-session:s1:0:1"]]


def test_a_rewind_is_not_a_departure(provider, drained, tmp_path):
    started(provider, tmp_path).on_session_switch("s1", rewound=True)
    assert drained == []


def test_a_cron_session_is_not_saved_on_the_switch(provider, drained, tmp_path):
    started(provider, tmp_path, agent_context="cron").on_session_switch("s2", reset=False)
    assert drained == []


# -- oura job ---------------------------------------------------------------------------------------------

def test_oura_rows_become_health_episodes_keyed_by_category_and_day():
    oura = load_job("oura_pull")
    daily = oura.episode("sleep", {"day": "2026-10-24", "score": 81}, oura.render_sleep)
    assert daily["source"] == {**daily["source"], "kind": "text", "external_id": "oura:sleep:2026-10-24"}
    assert daily["occurred_at"] == "2026-10-24T00:00:00Z"
    assert daily["sensitivity"] == ["health"] and daily["project"] == "health"
    assert daily["content"].startswith("Sleep 2026-10-24: score 81\n\n")
    workout = oura.episode("workout", {"day": "2026-10-24", "id": "w1", "start_datetime": "2026-10-24T07:00:00+02:00"},
                           oura.render_workout)
    assert workout["source"]["external_id"] == "oura:workout:2026-10-24:w1"
    assert workout["occurred_at"] == "2026-10-24T07:00:00+02:00"
    assert oura.episode("sleep", {"score": 1}, oura.render_sleep) is None


def test_sleep_renders_what_a_daily_sleep_row_has():
    oura = load_job("oura_pull")
    row = {"id": "x", "day": "2026-10-24", "score": 81, "timestamp": "2026-10-24T00:00:00.000+00:00",
           "contributors": {"deep_sleep": 70, "rem_sleep": 90}}  # the fields /daily_sleep returns
    assert oura.render_sleep(row) == ("Sleep 2026-10-24: score 81\n\ndate: 2026-10-24\nscore: 81\n"
                                      "contributors: {'deep_sleep': 70, 'rem_sleep': 90}")


def test_oura_run_reports_fetch_and_capture_failures():
    oura = load_job("oura_pull")

    def api(request):
        endpoint = request.url.path.rsplit("/", 1)[1]
        if endpoint == "daily_activity":
            return httpx.Response(500)
        if endpoint == "daily_sleep" and "next_token" not in request.url.params:
            return httpx.Response(200, json={"data": [{"day": "2026-10-23", "score": 80}], "next_token": "n"})
        if endpoint == "daily_sleep":
            return httpx.Response(200, json={"data": [{"day": "2026-10-24", "score": 82}]})
        return httpx.Response(200, json={"data": []})

    def capture(tool, args):
        if args["source"]["external_id"].endswith("10-24"):
            raise LookupError("refused")
        return stored(tool, args)
    transport, calls = mcp_server(capture)
    summary = oura.run(oura.Emerald("http://aeon/mcp", "k", transport=transport),
                       httpx.Client(transport=httpx.MockTransport(api)), days=3, today="2026-10-25")
    assert summary["window"] == ["2026-10-22", "2026-10-25"]
    assert summary["sent"] == {"sleep": 1, "readiness": 0, "workout": 0}
    assert [e.split(": ")[0] for e in summary["errors"]] == ["capture oura:sleep:2026-10-24", "fetch daily_activity"]
    assert [c[1]["source"]["external_id"] for c in calls] == ["oura:sleep:2026-10-23", "oura:sleep:2026-10-24"]


def test_oura_main_exits_1_when_anything_failed(tmp_path, monkeypatch, capsys):
    oura = load_job("oura_pull")
    (tmp_path / ".env").write_text("AEON_EMERALD_URL=http://aeon/mcp\nAEON_EMERALD_OURA_KEY=k\nOURA_PAT=t\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for errors, status in (([], 0), (["fetch daily_sleep: boom"], 1)):
        monkeypatch.setattr(oura, "run", lambda emerald, http, days, errors=errors: {"errors": errors})
        assert oura.main([]) == status
    assert '"errors": ["fetch daily_sleep: boom"]' in capsys.readouterr().out


# -- digest inputs ----------------------------------------------------------------------------------------

def freshrss(pages, seen=None):
    def api(request):
        if request.url.path.endswith("/accounts/ClientLogin"):
            body = dict(httpx.QueryParams(request.content.decode()))
            if body != {"Email": "admin", "Passwd": "pw"}:
                return httpx.Response(401, text="Unauthorized!")
            return httpx.Response(200, text="SID=x\nLSID=null\nAuth=admin/tok\n")
        assert request.url.path == "/api/greader.php/reader/api/0/stream/contents/user/-/state/com.google/reading-list"
        assert request.headers["authorization"] == "GoogleLogin auth=admin/tok"
        if seen is not None:
            seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("c", "")])
    return httpx.Client(transport=httpx.MockTransport(api))


def test_freshrss_items_follow_continuations_and_flatten_fields():
    digest = load_job("digest_inputs")
    item = {"title": "Big &amp; new", "origin": {"title": "Simon Willison"}, "crawlTimeMsec": "1792900000123",
            "categories": ["user/-/state/com.google/reading-list", "user/-/label/Tech"],
            "canonical": [{"href": "https://example.com/a"}], "summary": {"content": "<p>Some <b>text</b></p>"}}
    modified_old_item = {"title": "Edited last week", "crawlTimeMsec": "1792200000000"}  # FreshRSS 1.29+ ot
    pages = {"": {"items": [item, modified_old_item], "continuation": "next"},
             "next": {"items": [{"title": "Second", "crawlTimeMsec": "1792800000000"}]}}
    seen = []
    items = digest.freshrss_items(freshrss(pages, seen), "https://rss", "admin", "pw", 1792800000)
    assert items[0] == {"title": "Big & new", "feed": "Simon Willison", "category": "Tech",
                        "url": "https://example.com/a", "crawled": 1792900000, "snippet": "Some text"}
    assert [i["title"] for i in items] == ["Big & new", "Second"]
    assert items[1]["category"] == "Uncategorized" and items[1]["url"] is None
    assert [s.get("ot") for s in seen] == ["1792800000", "1792800000"] and seen[1]["c"] == "next"


def test_freshrss_login_failure_is_an_error():
    digest = load_job("digest_inputs")
    with pytest.raises(digest.SourceError, match="login: HTTP 401, no Auth token"):
        digest.freshrss_items(freshrss({}), "https://rss", "admin", "wrong", 1792800000)


def test_freshrss_login_without_an_auth_token_is_an_error():
    digest = load_job("digest_inputs")
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="Error=BadAuthentication\n")))
    with pytest.raises(digest.SourceError, match="HTTP 200, no Auth token"):
        digest.freshrss_items(http, "https://rss", "admin", "pw", 1792800000)


def test_watermark_repeats_the_day_moves_on_the_next_and_never_back(tmp_path):
    digest = load_job("digest_inputs")
    hours = digest.timedelta(hours=1)
    mark = digest.Watermark(tmp_path / "state" / "freshrss-watermark")
    morning = digest.datetime(2026, 10, 25, 8, tzinfo=digest.timezone.utc)
    first = mark.since(morning, 24)
    assert first == int((morning - 24 * hours).timestamp())
    mark.advance(morning, first, [{"crawled": 1792900000}, {"crawled": 1792900500}])
    retry = morning + hours / 2  # Hermes re-runs a digest whose model call failed
    assert mark.since(retry, 24) == first
    mark.advance(retry, first, [{"crawled": 1792900100}])
    tomorrow = morning + 24 * hours
    assert mark.since(tomorrow, 24) == 1792900501  # just after the newest item any run saw
    mark.advance(tomorrow, 1792900501, [])
    assert mark.since(tomorrow + 24 * hours, 24) == 1792900501  # an empty day keeps the mark
    later = morning + 30 * 24 * hours  # after a long pause, at most MAX_FEED_LOOKBACK
    assert mark.since(later, 24) == int((later - digest.MAX_FEED_LOOKBACK).timestamp())


def test_the_watermark_day_is_the_hosts_local_day(tmp_path, monkeypatch):
    digest = load_job("digest_inputs")
    monkeypatch.setenv("TZ", "Asia/Ho_Chi_Minh")  # UTC+7
    time.tzset()
    try:
        mark = digest.Watermark(tmp_path / "freshrss-watermark")
        evening = digest.datetime(2026, 10, 25, 16, tzinfo=digest.timezone.utc)  # 23:00 local, Oct 25
        mark.advance(evening, 100, [{"crawled": 500}])
        after_midnight = evening + digest.timedelta(hours=2)  # 01:00 local Oct 26, still Oct 25 in UTC
        assert mark.since(after_midnight, 24) != 100  # a new local day: the mark moves on
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_an_unreadable_watermark_is_an_error(tmp_path):
    digest = load_job("digest_inputs")
    (tmp_path / "freshrss-watermark").write_text("1792900001\n")
    with pytest.raises(digest.SourceError, match="unreadable watermark"):
        digest.Watermark(tmp_path / "freshrss-watermark").since(digest.datetime.now(digest.timezone.utc), 24)


def test_password_comes_only_from_the_freshrss_env_file(tmp_path, monkeypatch):
    digest = load_job("digest_inputs")
    (tmp_path / "freshrss.env").write_text("TZ=UTC\nADMIN_API_PASSWORD=from-file\n")
    (tmp_path / ".env").write_text(f"FRESHRSS_PASSWORD_FILE={tmp_path / 'freshrss.env'}\n"
                                   "FRESHRSS_PASSWORD_VAR=ADMIN_API_PASSWORD\n")
    monkeypatch.setenv("FRESHRSS_API_PASSWORD", "from-env")  # not a source
    assert digest.freshrss_password(tmp_path) == "from-file"


@pytest.mark.parametrize("freshrss_env, error", [
    (None, "not found"),
    ("TZ=UTC\n", "ADMIN_API_PASSWORD is not set in"),
])
def test_missing_password_source_is_reported(tmp_path, monkeypatch, capsys, freshrss_env, error):
    digest = load_job("digest_inputs")
    if freshrss_env is not None:
        (tmp_path / "freshrss.env").write_text(freshrss_env)
    (tmp_path / ".env").write_text(f"FRESHRSS_URL=https://rss\nFRESHRSS_USER=admin\n"
                                   f"FRESHRSS_PASSWORD_FILE={tmp_path / 'freshrss.env'}\n"
                                   "FRESHRSS_PASSWORD_VAR=ADMIN_API_PASSWORD\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("FRESHRSS_API_PASSWORD", "from-env")  # not a fallback
    assert digest.main([]) == 1
    assert error in capsys.readouterr().err


def test_missing_setting_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    digest = load_job("digest_inputs")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("FRESHRSS_URL", raising=False)
    assert digest.main([]) == 1
    assert "SettingMissing: FRESHRSS_URL is not set" in capsys.readouterr().err


def test_digest_reads_both_sources_and_advances_the_watermark(tmp_path, monkeypatch, capsys):
    digest = load_job("digest_inputs")
    (tmp_path / "freshrss.env").write_text("ADMIN_API_PASSWORD=pw\n")
    (tmp_path / ".env").write_text("FRESHRSS_URL=https://rss/\nFRESHRSS_USER=admin\n"
                                   f"FRESHRSS_PASSWORD_FILE={tmp_path / 'freshrss.env'}\n"
                                   "FRESHRSS_PASSWORD_VAR=ADMIN_API_PASSWORD\n"
                                   "AEON_EMERALD_URL=http://aeon/mcp\nAEON_EMERALD_KEY=k\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pages = {"": {"items": [{"title": "A", "crawlTimeMsec": "1792900000999", "origin": {"title": "F"}}]}}
    rss, real_client = freshrss(pages), httpx.Client
    # Only the FreshRSS client is built without a transport; Emerald's below gets the fake MCP one.
    monkeypatch.setattr(digest.httpx, "Client", lambda **kw: real_client(**kw) if "transport" in kw else rss)
    transport, _ = mcp_server(lambda tool, args: {"items": [], "from": args["from"], "to": args["to"]})
    real_emerald = digest.Emerald
    monkeypatch.setattr(digest.Emerald, "from_settings",
                        classmethod(lambda cls, home, key: real_emerald("http://aeon/mcp", "k", transport=transport)))
    assert digest.main([]) == 0
    assert "## Feeds (FreshRSS, 1 new items)" in capsys.readouterr().out
    assert json.loads((tmp_path / "aeon-emerald" / "freshrss-watermark").read_text())["next"] == 1792900001


def test_watermark_stays_put_when_emerald_cannot_be_read(tmp_path, monkeypatch, capsys):
    digest = load_job("digest_inputs")
    (tmp_path / "freshrss.env").write_text("ADMIN_API_PASSWORD=pw\n")
    (tmp_path / ".env").write_text("FRESHRSS_URL=https://rss/\nFRESHRSS_USER=admin\n"
                                   f"FRESHRSS_PASSWORD_FILE={tmp_path / 'freshrss.env'}\n"
                                   "FRESHRSS_PASSWORD_VAR=ADMIN_API_PASSWORD\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pages = {"": {"items": [{"title": "A", "crawlTimeMsec": "1792900000999", "origin": {"title": "F"}}]}}
    rss, real_client = freshrss(pages), httpx.Client
    monkeypatch.setattr(digest.httpx, "Client", lambda **kw: real_client(**kw) if "transport" in kw else rss)

    def down(tool, args):
        raise LookupError("unavailable")
    transport, _ = mcp_server(down)
    real_emerald = digest.Emerald
    monkeypatch.setattr(digest.Emerald, "from_settings",
                        classmethod(lambda cls, home, key: real_emerald("http://aeon/mcp", "k", transport=transport)))
    assert digest.main([]) == 1
    assert "timeline" in capsys.readouterr().err
    assert not (tmp_path / "aeon-emerald" / "freshrss-watermark").exists()


def test_timeline_pages_past_the_limit_without_duplicates(monkeypatch):
    digest = load_job("digest_inputs")
    monkeypatch.setattr(digest, "TIMELINE_PAGE", 2)
    rows = [{"kind": "episode", "id": f"e{i}", "text": f"t{i}", "at": f"2026-10-24T0{i}:00:00Z"} for i in range(3)]

    def timeline(tool, args):
        assert tool == "timeline"
        later = [r for r in rows if r["at"] >= args["from"]] if not args["from"].startswith("2026-10-23") else rows
        return {"items": later[:args["limit"]], "from": args["from"], "to": args["to"]}
    transport, _ = mcp_server(timeline)
    since = digest.datetime(2026, 10, 23, tzinfo=digest.timezone.utc)
    items, complete = digest.timeline_items(digest.Emerald("http://aeon/mcp", "k", transport=transport),
                                            since, since + digest.timedelta(days=2))
    assert [i["id"] for i in items] == ["e0", "e1", "e2"] and complete


def test_timeline_stops_when_a_page_brings_nothing_new(monkeypatch):
    digest = load_job("digest_inputs")
    monkeypatch.setattr(digest, "TIMELINE_PAGE", 2)
    same_time = [{"kind": "episode", "id": f"e{i}", "text": "t", "at": "2026-10-24T00:00:00Z"} for i in range(2)]
    transport, calls = mcp_server(lambda tool, args: {"items": same_time, "from": args["from"], "to": args["to"]})
    since = digest.datetime(2026, 10, 23, tzinfo=digest.timezone.utc)
    items, complete = digest.timeline_items(digest.Emerald("http://aeon/mcp", "k", transport=transport),
                                            since, since + digest.timedelta(days=2))
    assert [i["id"] for i in items] == ["e0", "e1"] and len(calls) == 2
    assert not complete  # more than a page share one timestamp: the rest cannot be paged to


def test_render_groups_feeds_and_separates_saved_items_from_facts():
    digest = load_job("digest_inputs")
    text = digest.render(
        [{"title": "A", "feed": "F", "category": "Tech", "url": "https://a", "crawled": 1, "snippet": "s"}],
        [{"kind": "episode", "id": "e", "text": "Saved note", "at": "2026-10-24T01:00:00Z"},
         {"kind": "claim", "id": "c", "text": "Vu prefers X", "at": "2026-10-24T01:00:00Z"}], 24)
    assert "## Feeds (FreshRSS, 1 new items)\n\n### Tech\n- A (F) <https://a>\n  s" in text
    assert "## Memory (Aeon, last 24 hours: 1 saved items, 1 new facts)" in text
    assert text.endswith("### New facts\n- Vu prefers X")


def test_render_says_when_the_timeline_is_incomplete():
    digest = load_job("digest_inputs")
    memory = [{"kind": "episode", "id": "e", "text": "Saved note", "at": "2026-10-24T01:00:00Z"}]
    assert "Incomplete" not in digest.render([], memory, 24)
    assert "(Incomplete: Aeon had more for this window than could be read; these are the first 1.)" in \
        digest.render([], memory, 24, memory_complete=False)
