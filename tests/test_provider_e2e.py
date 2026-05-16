"""End-to-end provider lifecycle against a tmp libSQL/SQLite db."""
import json

import pytest


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_AEON_TURSO_URL", raising=False)
    monkeypatch.delenv("HERMES_AEON_TURSO_TOKEN", raising=False)
    from hermes_aeon.provider import AeonMemoryProvider
    p = AeonMemoryProvider(config={
        "db_path": str(tmp_path / "aeon.db"),
        "embed_provider": "none",
        "extractor": "jina",
        "auto_extract": True,
    })
    p.initialize(session_id="test-session", hermes_home=str(tmp_path))
    yield p
    p.shutdown()


def test_provider_name_and_available(provider):
    assert provider.name == "hermes-aeon"
    assert provider.is_available() is True


def test_get_tool_schemas_returns_six(provider):
    schemas = provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert names == {
        "aeon_capture", "aeon_search", "aeon_calendar",
        "aeon_update", "aeon_set_tone", "aeon_get_tone",
    }


def test_capture_then_search_roundtrip(provider):
    cap = json.loads(provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "note",
        "title": "RRF fusion notes",
        "content": "Reciprocal rank fusion combines lexical and vector retrieval.",
    }))
    assert cap["status"] == "captured"
    mid = cap["id"]

    found = json.loads(provider.handle_tool_call("aeon_search", {
        "query": "reciprocal rank fusion", "domain": "learning",
    }))
    assert found["count"] >= 1
    ids = {r["id"] for r in found["results"]}
    assert mid in ids


def test_capture_rejects_missing_payload(provider):
    out = provider.handle_tool_call("aeon_capture", {"domain": "inbox", "type": "note"})
    assert "error" in out.lower()


def test_set_then_get_tone_persists(provider):
    set_out = json.loads(provider.handle_tool_call("aeon_set_tone", {"preset": "analyst", "weight": 1.0}))
    assert set_out["tone"]["directness"] >= 0.7
    get_out = json.loads(provider.handle_tool_call("aeon_get_tone", {}))
    assert get_out["tone"]["directness"] == set_out["tone"]["directness"]


def test_set_tone_axes_partial_blend(provider):
    out = json.loads(provider.handle_tool_call("aeon_set_tone", {"axes": {"warmth": 1.0}, "weight": 0.5}))
    # default warmth 0.6, target 1.0, weight 0.5 -> 0.8
    assert abs(out["tone"]["warmth"] - 0.8) < 1e-6


def test_prefetch_includes_tone_block(provider):
    block = provider.prefetch("hello")
    assert "Tone state" in block


def test_prefetch_returns_recall_when_match_exists(provider):
    provider.handle_tool_call("aeon_capture", {
        "domain": "work", "type": "note",
        "title": "deploy notes",
        "content": "the deploy script lives at scripts/deploy.sh",
    })
    block = provider.prefetch("deploy script location")
    assert "Aeon memory recall" in block


def test_calendar_filter_by_window(provider):
    provider.handle_tool_call("aeon_capture", {
        "domain": "work", "type": "calendar_event",
        "title": "standup",
        "content": "daily standup",
        "event_start_ms": 1_000_000_000_000,
        "event_end_ms":   1_000_000_900_000,
    })
    out = json.loads(provider.handle_tool_call("aeon_calendar", {
        "start_ms": 0, "end_ms": 2_000_000_000_000,
    }))
    assert out["count"] == 1


def test_update_appends_revision(provider):
    cap = json.loads(provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "note", "content": "v1 thought",
    }))
    upd = json.loads(provider.handle_tool_call("aeon_update", {
        "memory_id": cap["id"], "content": "v2 thought, refined",
    }))
    assert upd["revision"] == 2


def test_on_session_end_extracts_url(provider):
    provider.on_session_end([
        {"role": "user", "content": "interesting article: https://example.com/foo"},
    ])
    found = json.loads(provider.handle_tool_call("aeon_search", {
        "query": "example", "domain": "inbox", "type": "link",
    }))
    assert found["count"] >= 1


def test_system_prompt_block_is_string(provider):
    block = provider.system_prompt_block()
    assert isinstance(block, str)
