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
    general_socket: ""        # optional; e.g. /run/aeon-general/general.sock
    embed_provider: gemini    # gemini | none. 'none' uses FTS5-only retrieval.
    auto_extract: true
    extractor: jina           # jina | firecrawl
    turso_url: ""             # optional; libSQL embedded by default
    turso_token: ""
```

Env-var equivalents: `HERMES_AEON_TURSO_URL`, `HERMES_AEON_TURSO_TOKEN`, `GEMINI_API_KEY`.

## Agent-neutral general brain

General memory (`brain.general.v1`) is shared with the other agents. Two
transports are supported:

- **In-process (default)** — Hermes is the trusted store host; general ops run
  through the same `Gateway`/`Service` boundary the socket server uses, under
  the `hermes` principal.
- **Gateway client (`general_socket` set)** — Hermes behaves like every other
  bot: general recall, captures and revisions go through the socket as a
  `brain_service.client.Client`, attributed to its configured principal, and
  **fail closed** — an unavailable gateway never falls back to the canonical
  database. The canonical DB remains connected only for legacy ingest
  (feeds, bookmarks, URL captures) this milestone.

Plain notes captured into `work`/`learning`/`side_projects` go through
`brain.general.v1` in both modes: idempotent per payload (retries replay the
receipt), topics from `tags`, and revisable only by their capturing agent.
Other shapes (URL extractions, inbox, calendar, tasks) stay canonical records.

## Tools

- `aeon_capture(content?, url?, domain, type, title?, tags?, project_id?, record_class?)` — store a memory; plain notes in the general domains route through `brain.general.v1`, URL captures route through extractor + embed.
- `aeon_search(query, domain?, type?, project_id?, topic?, source?, record_class?, limit?)` — general-only lexical recall with provenance.
- `aeon_recent(hours?, limit?, offset?, min_score?, source?, domain?, order?, record_class?)` — time-windowed paginated lookup.
- `aeon_get(memory_id)` — one general record with full provenance; `found=false` for retracted/expired records.
- `aeon_update(memory_id, content, expected_revision, summary?)` — append a revision (creator-only for general records); stale revisions return a conflict.
- `aeon_propose(memory_id, expected_revision, statement, reason)` — propose a contradicting assertion on another agent's record without overwriting it.
- `aeon_retract(memory_id, expected_revision, reason)` — withdraw your own general record.
- `aeon_status()` — service contract and commit/index sequences.
- `aeon_calendar(start, end, domain?)` — unavailable until calendar lineage is supported.
- `aeon_set_tone(preset?, axes?, weight?)` / `aeon_get_tone()` — session voice.
- `aeon_pull_*`, `aeon_digest` — ingest fetchers and synthesis (canonical store).

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
Other agents use the `brain.general.v1` gateway directly — see
[docs/general-service-rollout.md](docs/general-service-rollout.md); the Hermes
plugin joins them via the `general_socket` option above.

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
