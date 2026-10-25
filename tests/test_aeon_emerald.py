"""aeon_emerald: Emerald client, session transcripts, provider, and the cron jobs, against fake HTTP."""
import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "aeon_emerald"

client = importlib.import_module("hermes_aeon.aeon_emerald.client")
session = importlib.import_module("hermes_aeon.aeon_emerald.session")


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
    monkeypatch.delitem(sys.modules, "hermes_aeon.aeon_emerald.provider", raising=False)
    return importlib.import_module("hermes_aeon.aeon_emerald.provider")


def load_job(name):
    spec = importlib.util.spec_from_file_location(f"aeon_emerald_job_{name}", PACKAGE / "jobs" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event_stream(*messages):
    """A server-sent event reply, the way Emerald answers a client that accepts both formats."""
    text = "".join(f"event: message\ndata: {json.dumps(m)}\n\n" for m in messages)
    return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


def mcp_server(handler):
    """A fake Emerald /mcp endpoint: ``handler(tool, arguments)`` returns structured content or raises."""
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        params = body["params"]
        calls.append((params["name"], params["arguments"], request.headers["authorization"]))
        try:
            value = handler(params["name"], params["arguments"])
        except LookupError as error:
            return event_stream({"jsonrpc": "2.0", "id": body["id"], "result": {
                "isError": True, "content": [{"type": "text", "text": str(error)}],
                "structuredContent": {"code": "NotFound", "message": str(error)}}})
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
    with pytest.raises(client.EmeraldError, match="fetch: .*NotFound"):
        client.Emerald("http://aeon/mcp", "k", transport=transport).call("fetch", {"id": "x"})


def test_call_reads_plain_json_replies():
    def reply(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"],
                                         "result": {"structuredContent": {"items": []}}})
    emerald = client.Emerald("http://aeon/mcp", "k", transport=httpx.MockTransport(reply))
    assert emerald.call("timeline", {}) == {"items": []}


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
    with pytest.raises(client.EmeraldError, match="AEON_C is not set"):
        client.setting("AEON_C", tmp_path)


# -- session transcripts --------------------------------------------------------------------------------

MESSAGES = [
    {"role": "system", "content": "You are Hermes."},
    {"role": "user", "content": "What did we decide about the VPS?"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
    {"role": "tool", "content": "{\"huge\": \"tool output\"}"},
    {"role": "assistant", "content": [{"type": "text", "text": "We moved Hermes to Emerald."}]},
]


def test_transcript_keeps_only_user_and_assistant_text():
    assert session.transcript_turns(MESSAGES) == [
        "User: What did we decide about the VPS?", "Hermes: We moved Hermes to Emerald."]


def test_sessions_without_user_text_are_not_captured():
    assert session.session_captures("s1", [{"role": "assistant", "content": "Good morning"}]) == []


def test_long_transcripts_split_at_turn_boundaries_and_oversized_turns_are_cut():
    messages = [{"role": "user", "content": "a" * 40}, {"role": "assistant", "content": "b" * 40},
                {"role": "user", "content": "c" * 130}]
    parts = session.transcript_parts(messages, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert parts[0] == f"User: {'a' * 40}\n\nHermes: {'b' * 40}"
    assert "".join(parts[1:]) == f"User: {'c' * 130}"


def test_session_captures_name_parts_and_thread_by_session():
    one = session.session_captures("s1", MESSAGES, platform="telegram")
    assert len(one) == 1
    assert one[0]["source"] == {**one[0]["source"], "kind": "transcript", "external_id": "hermes-session:s1"}
    assert one[0]["thread_id"] == "s1"
    assert one[0]["content"].startswith("Hermes session s1 (telegram)\n\nUser: ")
    long = [{"role": "user", "content": "x" * (session.PART_LIMIT + 10)}]
    ids = [c["source"]["external_id"] for c in session.session_captures("s2", long)]
    assert ids == ["hermes-session:s2:part1", "hermes-session:s2:part2"]


def test_failed_captures_are_spooled_and_sent_on_a_later_session_end(tmp_path):
    spool = session.Spool(tmp_path / "spool")
    captures = session.session_captures("s1", MESSAGES)

    def down(tool, args):
        raise LookupError("unavailable")
    transport, _ = mcp_server(down)
    assert session.send(client.Emerald("http://aeon/mcp", "k", transport=transport), spool, captures) == 1
    assert [p.stem for p in spool.pending()] == [captures[0]["operation_id"]]

    transport, calls = mcp_server(stored)
    assert session.send(client.Emerald("http://aeon/mcp", "k", transport=transport), spool, []) == 0
    assert spool.pending() == []
    assert calls[0][1] == captures[0]


# -- provider ---------------------------------------------------------------------------------------------

def test_provider_captures_primary_sessions_only(provider, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(provider.Emerald, "from_settings", classmethod(lambda cls, home, key: object.__new__(cls)))
    monkeypatch.setattr(provider.Emerald, "close", lambda self: None)
    monkeypatch.setattr(provider, "send", lambda c, spool, captures: sent.append(captures) or 0)

    cron = provider.EmeraldSessionProvider()
    cron.initialize("s-cron", hermes_home=str(tmp_path), platform="cron", agent_context="cron")
    cron.on_session_end(MESSAGES)
    assert sent == []

    chat = provider.EmeraldSessionProvider()
    chat.initialize("s1", hermes_home=str(tmp_path), platform="telegram")
    chat.on_session_switch("s2")
    chat.on_session_end(MESSAGES)
    assert [c["source"]["external_id"] for c in sent[0]] == ["hermes-session:s2"]
    assert chat.get_tool_schemas() == []


def test_provider_spools_when_settings_are_missing(provider, tmp_path):
    chat = provider.EmeraldSessionProvider()
    chat.initialize("s1", hermes_home=str(tmp_path), platform="telegram")
    chat.on_session_end(MESSAGES)
    assert len(session.Spool(tmp_path / "aeon-emerald" / "spool").pending()) == 1


# -- oura job ---------------------------------------------------------------------------------------------

def test_oura_rows_become_health_episodes_keyed_by_category_and_day():
    oura = load_job("oura_pull")
    daily = oura.episode("sleep", {"day": "2026-10-24", "score": 81}, oura.render_sleep)
    assert daily["external_id"] == "oura:sleep:2026-10-24"
    assert daily["occurred_at"] == "2026-10-24T00:00:00Z"
    assert daily["sensitivity"] == ["health"] and daily["project"] == "health"
    assert daily["content"].startswith("Sleep 2026-10-24: score 81\n\n")
    workout = oura.episode("workout", {"day": "2026-10-24", "id": "w1", "start_datetime": "2026-10-24T07:00:00+02:00"},
                           oura.render_workout)
    assert workout["external_id"] == "oura:workout:2026-10-24:w1"
    assert workout["occurred_at"] == "2026-10-24T07:00:00+02:00"
    assert oura.episode("sleep", {"score": 1}, oura.render_sleep) is None


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


# -- digest inputs ----------------------------------------------------------------------------------------

def freshrss(pages):
    def api(request):
        if request.url.path.endswith("/accounts/ClientLogin"):
            body = dict(httpx.QueryParams(request.content.decode()))
            if body != {"Email": "admin", "Passwd": "pw"}:
                return httpx.Response(401, text="Unauthorized!")
            return httpx.Response(200, text="SID=x\nLSID=null\nAuth=admin/tok\n")
        assert request.headers["authorization"] == "GoogleLogin auth=admin/tok"
        return httpx.Response(200, json=pages[request.url.params.get("c", "")])
    return httpx.Client(transport=httpx.MockTransport(api))


def test_freshrss_items_follow_continuations_and_flatten_fields():
    digest = load_job("digest_inputs")
    item = {"title": "Big &amp; new", "origin": {"title": "Simon Willison"},
            "categories": ["user/-/state/com.google/reading-list", "user/-/label/Tech"],
            "canonical": [{"href": "https://example.com/a"}], "summary": {"content": "<p>Some <b>text</b></p>"}}
    pages = {"": {"items": [item], "continuation": "next"}, "next": {"items": [{"title": "Second"}]}}
    items = digest.freshrss_items(freshrss(pages), "https://rss", "admin", "pw",
                                  digest.datetime(2026, 10, 24, tzinfo=digest.timezone.utc))
    assert items[0] == {"title": "Big & new", "feed": "Simon Willison", "category": "Tech",
                        "url": "https://example.com/a", "published": None, "snippet": "Some text"}
    assert items[1]["category"] == "Uncategorized" and items[1]["url"] is None


def test_freshrss_login_failure_is_an_error():
    digest = load_job("digest_inputs")
    with pytest.raises(digest.SourceError, match="login: HTTP 401"):
        digest.freshrss_items(freshrss({}), "https://rss", "admin", "wrong",
                              digest.datetime(2026, 10, 24, tzinfo=digest.timezone.utc))


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
    items = digest.timeline_items(digest.Emerald("http://aeon/mcp", "k", transport=transport),
                                  since, since + digest.timedelta(days=2))
    assert [i["id"] for i in items] == ["e0", "e1", "e2"]


def test_render_groups_feeds_and_separates_saved_items_from_facts():
    digest = load_job("digest_inputs")
    text = digest.render(
        [{"title": "A", "feed": "F", "category": "Tech", "url": "https://a", "published": 1, "snippet": "s"}],
        [{"kind": "episode", "id": "e", "text": "Saved note", "at": "2026-10-24T01:00:00Z"},
         {"kind": "claim", "id": "c", "text": "Vu prefers X", "at": "2026-10-24T01:00:00Z"}], 24)
    assert "## Feeds (FreshRSS, 1 items)\n\n### Tech\n- A (F) <https://a>\n  s" in text
    assert "## Memory (Aeon: 1 saved items, 1 new facts)" in text
    assert text.endswith("### New facts\n- Vu prefers X")


def test_digest_fails_loudly_when_a_source_is_not_configured(tmp_path, monkeypatch, capsys):
    digest = load_job("digest_inputs")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("FRESHRSS_URL", "FRESHRSS_USER", "FRESHRSS_API_PASSWORD", "AEON_EMERALD_URL", "AEON_EMERALD_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert digest.main([]) == 1
    assert "FRESHRSS_URL is not set" in capsys.readouterr().err
