"""End-to-end provider lifecycle against a tmp libSQL/SQLite db."""
import json

import pytest


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_AEON_TURSO_URL", raising=False)
    monkeypatch.delenv("HERMES_AEON_TURSO_TOKEN", raising=False)
    pytest.importorskip("agent.memory_provider", reason="provider lifecycle requires the Hermes host runtime")
    from hermes_aeon.provider import AeonMemoryProvider
    # Never perform URL extraction over the network during lifecycle tests.
    monkeypatch.setattr("hermes_aeon.provider.jina.extract", lambda url: "# Synthetic article\n" + url)
    p = AeonMemoryProvider(config={
        "db_path": str(tmp_path / "aeon.db"),
        "embed_provider": "none",
        "extractor": "jina",
        "auto_extract": True,
    })
    p.initialize(session_id="test-session", hermes_home=str(tmp_path))
    # Migration is an explicit operator step; never automatic during a request.
    from pathlib import Path
    sql=(Path(__file__).resolve().parent.parent / "aeon_sharedwrites/migration.sql").read_text()
    for statement in sql.split(';'):
        if statement.strip():p._db.execute(statement)
    sql=(Path(__file__).resolve().parent.parent / "brain_service/migration.sql").read_text()
    for statement in sql.split(';'):
        if statement.strip():p._db.execute(statement)
    p._db.commit()
    yield p
    p.shutdown()


def test_provider_name_and_available(provider):
    assert provider.name == "hermes-aeon"
    assert provider.is_available() is True


def test_get_tool_schemas_returns_all(provider):
    schemas = provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    expected = {
        # Memory CRUD + query
        "aeon_capture", "aeon_search", "aeon_calendar", "aeon_recent", "aeon_update",
        # Tone
        "aeon_set_tone", "aeon_get_tone",
        # Ingest (agent-triggerable fetchers)
        "aeon_pull_bookmarks", "aeon_pull_github",
        "aeon_pull_rss", "aeon_pull_x", "aeon_pull_hf_papers", "aeon_pull_hype",
        "aeon_digest",
    }
    assert names == expected, f"missing: {expected - names}; extra: {names - expected}"


def test_system_prompt_keeps_active_context_after_newer_imports(provider):
    from hermes_aeon.brain_service.policy import HERMES
    from hermes_aeon.brain_service.service import Service, now_ms
    from hermes_aeon.store import queries
    brain=Service(provider._db)
    mid=brain.capture(HERMES,dict(request_id='active-context',statement='Synthetic active compiler project',
                                 domain='work',record_class='working_context'))['memory_id']
    provider._db.execute('UPDATE memory_items SET captured_at=? WHERE id=?',(now_ms()-1000,mid))
    provider._db.commit()
    for number in range(101):
        queries.capture_memory(provider._db,type='note',domain='learning',content='Synthetic newer feed item',
                               source='discover:hn',request_id='feed'+str(number))
    assert 'Synthetic active compiler project' in provider.system_prompt_block()


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


def test_prefetch_returns_recall_only(provider):
    provider.handle_tool_call("aeon_capture", {
        "domain": "work", "type": "note",
        "title": "deploy notes",
        "content": "the deploy script lives at scripts/deploy.sh",
    })
    block = provider.prefetch("deploy script location")
    assert "Aeon memory recall" in block
    assert "Tone state" not in block  # tone is injected via the pre_llm_call hook, not here


def test_pre_llm_call_hook_returns_tone_block(provider):
    """The hook reads ToneStore from disk and returns a {context: ...} dict."""
    from hermes_aeon.tone.hook import on_pre_llm_call
    out = on_pre_llm_call(session_id="test-session", user_message="hello")
    assert out is not None
    assert "context" in out
    assert "Tone state" in out["context"]


def test_pre_llm_call_hook_shares_state_with_provider(provider):
    """Provider and hook share state via the same ToneStore JSON file on disk."""
    from hermes_aeon.tone.hook import on_pre_llm_call
    # Provider writes tone via its tool
    json.loads(provider.handle_tool_call("aeon_set_tone", {"preset": "analyst", "weight": 1.0}))
    # Hook (file-backed) reads the same value
    out = on_pre_llm_call(session_id="test-session", user_message="hi")
    assert "directness: 0.80" in out["context"]


def test_pre_llm_call_hook_derives_tone_from_message(provider):
    from hermes_aeon.tone.hook import on_pre_llm_call
    out = on_pre_llm_call(session_id="test-session", user_message="coach me through this")
    # The hook persisted the derived state; the provider sees it via its store too
    new_tone = json.loads(provider.handle_tool_call("aeon_get_tone", {}))["tone"]
    assert new_tone["warmth"] > 0.6
    assert "Tone state" in out["context"]


def test_recent_orders_by_score_with_unscored_last(provider):
    # Three captures: 2 scored, 1 unscored
    a = json.loads(provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "link", "title": "A", "content": "a"}))["id"]
    b = json.loads(provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "link", "title": "B", "content": "b"}))["id"]
    c = json.loads(provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "link", "title": "C", "content": "c"}))["id"]
    # Hand-set quality_scores on A and C via direct DB update (capture tool doesn't set it)
    provider._db.execute("UPDATE memory_items SET quality_score = 0.9 WHERE id = ?", (a,))
    provider._db.execute("UPDATE memory_items SET quality_score = 0.6 WHERE id = ?", (c,))
    provider._db.commit()

    out = json.loads(provider.handle_tool_call("aeon_recent", {"hours": 1, "limit": 10}))
    titles = [it["title"] for it in out["items"]]
    # A (0.9) before C (0.6); B (unscored) last
    assert titles.index("A") < titles.index("C") < titles.index("B")


def test_recent_filters_by_min_score(provider):
    provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "link", "title": "low", "content": "x"})
    provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "link", "title": "high", "content": "y"})
    high_id = provider._db.execute("SELECT id FROM memory_items WHERE title='high'").fetchone()[0]
    provider._db.execute("UPDATE memory_items SET quality_score = 0.8 WHERE id = ?", (high_id,))
    provider._db.commit()

    out = json.loads(provider.handle_tool_call("aeon_recent", {"min_score": 0.7}))
    titles = [it["title"] for it in out["items"]]
    assert titles == ["high"]


def test_recent_paginates(provider):
    for i in range(5):
        provider.handle_tool_call("aeon_capture", {
            "domain": "learning", "type": "link", "title": f"item{i}", "content": "x"})

    p1 = json.loads(provider.handle_tool_call("aeon_recent", {"limit": 2, "offset": 0}))
    p2 = json.loads(provider.handle_tool_call("aeon_recent", {"limit": 2, "offset": p1["next_offset"]}))
    assert p1["total"] >= 5
    assert len(p1["items"]) == 2
    assert len(p2["items"]) == 2
    p1_ids = {it["id"] for it in p1["items"]}
    p2_ids = {it["id"] for it in p2["items"]}
    assert not (p1_ids & p2_ids), "pages must not overlap"
    assert p1["has_more"] is True


def test_recent_filters_by_source_prefix(provider):
    # Use direct DB inserts to control source values
    provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "link", "title": "A", "content": "x"})
    provider._db.execute(
        "UPDATE memory_items SET source = 'discover:hn' WHERE title = 'A'")
    provider._db.commit()  # shared writes own their transaction
    provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "link", "title": "B", "content": "y"})
    provider._db.execute(
        "UPDATE memory_items SET source = 'discover:x' WHERE title = 'B'")
    provider._db.commit()

    out_all = json.loads(provider.handle_tool_call("aeon_recent", {"source": "discover:"}))
    titles_all = {it["title"] for it in out_all["items"]}
    assert {"A", "B"}.issubset(titles_all)

    out_hn = json.loads(provider.handle_tool_call("aeon_recent", {"source": "discover:hn"}))
    titles_hn = {it["title"] for it in out_hn["items"]}
    assert "A" in titles_hn
    assert "B" not in titles_hn


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
    assert "unavailable" in str(out).lower()


def test_update_appends_revision(provider):
    cap = json.loads(provider.handle_tool_call("aeon_capture", {
        "domain": "learning", "type": "note", "content": "v1 thought",
    }))
    upd = json.loads(provider.handle_tool_call("aeon_update", {
        "memory_id": cap["id"], "content": "v2 thought, refined", "expected_revision": 1,
    }))
    assert upd["revision"] == 2


def test_on_session_end_extracts_url(provider):
    provider.on_session_end([
        {"role": "user", "content": "interesting article: https://example.com/foo"},
    ])
    found = json.loads(provider.handle_tool_call("aeon_search", {
        "query": "example", "domain": "inbox", "type": "link",
    }))
    assert "error" in str(found).lower()
    # Stored legacy inbox evidence is not admitted to the initial GENERAL corpus.
    assert provider._db.execute("SELECT count(*) FROM memory_items WHERE domain='inbox'").fetchone()[0]>=1


def test_system_prompt_block_is_string(provider):
    block = provider.system_prompt_block()
    assert isinstance(block, str)


def test_general_routes_exclude_synthetic_health_metadata_and_unknown_context(provider,monkeypatch):
    from hermes_aeon.store import queries as q
    canary='SYNTHETIC_RESTRICTED_CANARY'
    q.capture_memory(provider._db,type='note',domain='health',content=canary,source='oura:synthetic')
    q.capture_memory(provider._db,type='note',domain='work',content='Harmless project',entities={'detail':'medical '+canary},source='manual')
    provider._db.execute('INSERT INTO tom_cards VALUES(?,?,?,?,?,?)',('tom','local','work',canary,9999999999999,1))
    provider._db.execute('INSERT INTO capsules VALUES(?,?,?,?,?,?,?,?)',('capsule','local','work','daily',1,2,canary,1))
    provider._db.commit()
    assert canary not in provider.system_prompt_block()
    assert canary not in provider.prefetch('project restricted canary')
    for name,args in [('aeon_search',{'query':'canary project'}),('aeon_recent',{}),('aeon_derive_profile',{}),('aeon_pull_oura',{}),('aeon_calendar',{'start_ms':0,'end_ms':9999999999999})]:
        assert canary not in provider.handle_tool_call(name,args)
    captured=[]
    from hermes_aeon.ingest import digest as digest_module
    monkeypatch.setattr(digest_module,'llm_chat',lambda prompt,**kwargs:captured.append(prompt) or 'Synthetic general digest')
    digest=provider.handle_tool_call('aeon_digest',{})
    assert canary not in digest
    assert all(canary not in prompt for prompt in captured)


def test_digest_revalidates_general_input_revisions(provider,monkeypatch):
    from hermes_aeon.store import queries as q
    from hermes_aeon.ingest import digest
    mid=q.capture_memory(provider._db,type='note',domain='work',content='General project input',source='manual')
    def synthesize(prompt,**kwargs):
        q.update_memory_content(provider._db,memory_id=mid,expected_revision=1,content='medical restricted update',summary=None,source='manual')
        return 'Derived stale result'
    monkeypatch.setattr(digest,'llm_chat',synthesize)
    result=digest.run(provider._db)
    assert result['text']==''
    assert result['error']=='general_inputs_changed'


def test_missing_general_gate_has_no_canonical_recall_fallback(provider):
    provider._db.execute('DROP TABLE brain_records');provider._db.commit()
    assert provider.prefetch('Synthetic project')==''
    assert provider.system_prompt_block()==''
    out=provider.handle_tool_call('aeon_search',{'query':'project'})
    assert 'error' in out.lower()
