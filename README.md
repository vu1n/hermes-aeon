# hermes-aeon

Aeon memory provider for [Hermes-Agent](https://github.com/NousResearch/hermes-agent). Personal knowledge base with 7 domains × 10 types, hybrid vector+FTS retrieval, append-only revisions, and a 5-axis tone codec — all local-first via libSQL with optional Turso sync.

## Install

```bash
git clone https://github.com/vu1n/hermes-aeon ~/code/hermes-aeon
ln -s ~/code/hermes-aeon ~/.hermes/plugins/hermes-aeon
pip install -e ~/code/hermes-aeon

hermes config set memory.provider hermes-aeon
```

## Configure

`~/.hermes/config.yaml`:

```yaml
plugins:
  hermes-aeon:
    db_path: $HERMES_HOME/aeon.db
    embed_provider: gemini    # gemini | none. 'none' uses FTS5-only retrieval.
    auto_extract: true
    extractor: jina           # jina | firecrawl
    turso_url: ""             # optional; libSQL embedded by default
    turso_token: ""
```

Env-var equivalents: `HERMES_AEON_TURSO_URL`, `HERMES_AEON_TURSO_TOKEN`, `GEMINI_API_KEY`.

## Tools

- `aeon_capture(content?, url?, domain, type, title?, tags?, project_id?)` — store a memory; URL captures route through extractor + embed.
- `aeon_search(query, domain?, type?, project_id?, limit?)` — hybrid vector + FTS5 with RRF.
- `aeon_calendar(start, end, domain?)` — calendar-event slice.
- `aeon_update(memory_id, content, expected_revision, summary?, source?)` — append a revision; stale revisions return a conflict.
- `aeon_set_tone(preset?, axes?, weight?)` — adjust the agent's voice for the session.
- `aeon_get_tone()` — read current tone state.

## Concepts

- **Domains**: inbox, work, side_projects, learning, health, life_admin, people_comms.
- **Types**: note, link, email, thread, task, health_log, file, calendar_event, contact, other.
- **Statuses**: active, archived, quarantined.
- **Revisions**: append-only history per memory; the canonical row carries the latest snapshot.
- **TOM cards**: volatile/expiring per-domain context surfaced in the system prompt.
- **Capsules**: period-bounded summaries (daily/weekly/monthly) per domain.

See PLAN.md for the architecture rationale.

## Optional shared brain

The repository includes a filtered read-only MCP adapter (`aeon_readonly`) and
a private shared-write broker (`aeon_sharedwrites`). The broker and Hermes use
the same canonical transactional writer in `store/shared_writer.py`; the
projection is a filtered transport view, not a second writable memory store.

The write ledger requires an explicit migration before the patched Hermes
writer accepts captures or updates. For a new installation, initialize the base
schema first; then apply the migration as described in
[docs/shared-brain-deployment.md](docs/shared-brain-deployment.md).
Installing or publishing this code does not authorize a migration, grant access,
enable services or configure a tunnel. Back up and coordinate all writers before
upgrading an existing deployment.

See [docs/shared-brain.md](docs/shared-brain.md) for scopes and consistency,
[docs/readonly-brain.md](docs/readonly-brain.md) for the read-only option, and
[docs/testing.md](docs/testing.md) for synthetic checks.
