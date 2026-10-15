# Aeon Brain over MCP

The brain is a standalone MCP service. Hermes is one client, not the host: any
MCP-capable agent (Claude, an IDE, a custom bot) gets the same tool surface,
the same provenance and receipts, and its own principal. This is the primary
interface going forward; the Hermes plugin (`general_socket` mode) and
`brain_service.client` are clients of the same brain.general.v1 contract.

## Surfaces

| Entry point | Access | Identity |
| --- | --- | --- |
| `python3 -m aeon_brain.mcp --db PATH [--bindings F \| --principal-name N]` | Trusted local: owns the canonical DB | This process's UID via bindings file, or the inline principal |
| `python3 -m aeon_brain.mcp --socket PATH` | Proxy: no DB access | Gateway daemon resolves this process's OS credentials |
| `python3 -m aeon_brain.server --db D --socket S --bindings B` | Gateway daemon (brain.general.v1) | SO_PEERCRED UID bindings |

All modes expose the same bounded tool surface (`aeon_capture`, `aeon_search`,
`aeon_recent`, `aeon_get`, `aeon_correct`, `revise`, `propose`, `retract`,
`status`, `interests`, consolidation and review operations). Results keep the
brain envelope `{ok, result}` / `{ok: false, error}`; MCP `isError` marks
logical failures so revision conflicts stay structured for the calling agent.
Frames are bounded at 64 KiB; oversized requests fail closed.

The servers never create, migrate or bootstrap a database. A missing general
schema is a refusal to start. Run from the operator repository root; the MCP
server imports no Hermes runtime code.

## Identity

Capabilities are trusted operator configuration, never client input. Distinct
bots need distinct OS UIDs for independent attribution; the gateway resolves
peer UIDs with SO_PEERCRED, and direct-mode MCP servers resolve their own UID
against the same bindings format:

```json
[
  {"uid": 0,    "id": "hermes",  "source_namespace": "chat:hermes",
   "capabilities": ["read", "capture", "revise_own", "retract_own", "propose"]},
  {"uid": 2101, "id": "planner", "source_namespace": "chat:planner",
   "capabilities": ["read", "capture", "revise_own", "retract_own", "propose"]}
]
```

Only the creator can `revise`/`retract`; cross-agent disagreement is `propose`.
Durable capture can return `search_visibility: ineligible` — storage is not
recall. Retry uncertain operations with the same `request_id`; a changed
payload under the same ID conflicts.

## Deployment shape (VPS)

- Canonical DB: `/var/lib/aeon-canonical/aeon.db` (root-owned, 0600, dir 0700).
  The Hermes home keeps a symlink so legacy ingest cadences keep working; the
  brain no longer lives inside any agent's home.
- Gateway daemon: `aeon-brain.service` (system scope), socket
  `/run/aeon-general/general.sock` (0600; widen with an ExecStartPost chgrp for
  non-root bots and map their UIDs in the bindings file).
- Direct-mode MCP (single-bot hosts): any MCP client config spawning
  `python3 -m aeon_brain.mcp --db /var/lib/aeon-canonical/aeon.db` as the
  operator account.

Example MCP client configuration (direct mode):

```json
{"mcpServers": {"aeon-brain": {
  "command": "python3", "args": ["-m", "aeon_brain.mcp", "--db", "/var/lib/aeon-canonical/aeon.db"],
  "cwd": "/opt/hermes-aeon"}}}
```

Example MCP client configuration (proxy mode for a non-root bot):

```json
{"mcpServers": {"aeon-brain": {
  "command": "python3", "args": ["-m", "aeon_brain.mcp", "--socket", "/run/aeon-general/general.sock"],
  "cwd": "/opt/hermes-aeon"}}}
```

## Migration and rollback

1. Pause all canonical writers (agent gateway, cron ingest). Take a consistent
   SQLite backup including WAL state; verify with `integrity_check`.
2. Checkpoint WAL, move the database and sidecars to the canonical directory,
   and leave a symlink at the old path so existing consumers keep working.
   Never run two writable copies; delete the moved-away original only after
   verification (backups remain).
3. Install the bindings file, start the gateway daemon, verify `status`,
   capture/replay/revise/conflict/retract with synthetic records.
4. Point clients at the brain (Hermes: `plugins.hermes-aeon.general_socket`;
   MCP hosts: the configuration above). Unavailable brain operations fail
   closed — no client ever falls back to raw database access.
5. Roll back: stop clients from the brain, stop the daemon, restore the old
   path from the moved-away file under the same writer pause. Additive schema
   and receipts can remain. Restoring a stale backup would discard memories.
