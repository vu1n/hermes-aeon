# Opt-in agent-neutral gateway

Aeon owns `brain.general.v1`, revision/CAS, provenance, receipts, indexing and
live eligibility. Hermes is a trusted in-process compatibility adapter. Other
agents use `brain_service.client.Client` or MCP; neither receives a DB handle.
The repository still packages the Hermes provider; splitting distribution,
remote authentication and health access are outside this milestone.

## Contract and synthetic second client

Run from an operator-installed repository root. No Hermes installation/import
is required by the gateway or client. A neutral planning/research agent can use:

```python
from brain_service.client import Client
brain = Client('/run/aeon-general/general.sock')
receipt = brain.call('capture', {
    'request_id': 'planner-project-001', 'domain': 'work',
    'statement': 'Compiler project uses reproducible builds',
    'topics': ['builds'], 'record_class': 'working_context',
})
record = brain.call('get', {'id': receipt['result']['memory_id']})
```

A second bound research agent can read that record and `propose` a linked claim;
only the creator can `revise` or `retract`. Revisions require `memory_id`,
`expected_revision`, `request_id`, `reason`, and a nonempty `patch`. Proposals
use those identity/revision fields with `statement` instead of `patch`.
`aeon_correct` preserves the legacy correction shape. Retry identical requests
with the same ID after an uncertain response; changing payload requires a new ID.
Attribution such as `user_explicit` remains a caller claim, never verified owner
attestation. Durable capture can return `search_visibility: ineligible`; callers
must inspect visibility, rather than equating storage with eligible general recall.

All general reads use current policy including private/health screening, expiry,
retraction and pinned lineage. Unknown legacy records stay excluded. Server-side
capabilities gate imports and derived views; this MCP surface advertises assertion
and working-context capture only. No arbitrary client source/owner/actor fields
are admitted. Socket frames are bounded at 64 KiB in both directions; oversized
results fail closed with `general_unavailable`. Use smaller query limits if needed.

## Proposed production configuration (not applied)

Linux only; SO_PEERCRED maps OS UIDs to trusted principals. Distinct agents need
distinct OS UIDs for independent attribution. Running all clients under one UID
provides one principal, regardless of caller-reported names. Bindings and code
must be owned by the operator and unwritable by client accounts. Example trusted
bindings file, with placeholder UIDs to resolve before approval:

```json
[
  {"uid": 2101, "id": "planner", "source_namespace": "chat:planner",
   "capabilities": ["read", "capture", "revise_own", "retract_own", "propose"]},
  {"uid": 2102, "id": "researcher", "source_namespace": "chat:researcher",
   "capabilities": ["read", "capture", "revise_own", "retract_own", "propose"]}
]
```

After separately approved provisioning, an operator could run:

```sh
python3 -m brain_service.server --db /var/lib/aeon/aeon.db \
  --socket /run/aeon-general/general.sock --bindings /etc/aeon/general-bindings.json
python3 -m aeon_sharedwrites.mcp_adapter --general-socket /run/aeon-general/general.sock
python3 -m brain_service.client --socket /run/aeon-general/general.sock status '{}'
```

The server opens an existing database in `mode=rw`; it never creates/migrates it.
It creates a 0600 socket in an operator-managed directory and refuses to replace
an existing path. Granting group access for multiple UIDs, setting a supervised
service account, directory ownership and lifecycle are separate approved rollout
steps. Clients must have socket access only, no canonical DB, backup, credentials
or execution access in the service account. The service account necessarily has
canonical access and remains trusted. Use local storage; this entrypoint does not
load Hermes/Turso environment configuration or perform network sync. It handles
requests serially with five-second socket deadlines; high concurrency and remote
clients require a later transport design.

MCP general mode rejects `--db` and `--broker-socket`. Existing snapshot/broker
mode and its CLI arguments remain the default, unchanged. An unavailable general
socket never falls back to that mode. General read results use `{ok, result}`;
logical failures use `{ok: false, error}` and MCP `isError: true`. Consumers must
handle this service envelope rather than assuming legacy snapshot result shapes.

## Migration, validation and rollback proposal

1. Separately approve principals, filesystem isolation, supervisor, backup and
   exact DB path. Pause all canonical writers and take a consistent SQLite backup
   including WAL state (SQLite backup API or approved equivalent).
2. Apply the existing shared-writer migration if absent, then explicitly run
   `python3 -m brain_service.migrate --db <approved-existing-db>`. This is additive
   and admits no legacy records. Validate schema and synthetic records on a
   disposable copy first; do not use personal/health records as test fixtures.
3. Start the gateway behind a new local socket, validate with synthetic capture,
   get, revision, replay, proposal and retraction, then opt selected clients into
   `--general-socket`. Check receipt visibility and commit/index sequences.
   Keep the live tunnel and legacy consumers unchanged until separately approved.
4. Roll back consumer configuration and stop the new gateway. Leave additive
   schema and durable writes intact; legacy projections retain their own filters
   and do not automatically gain new sources. Do not drop tables on a live DB.
   Restoring a backup is a separate destructive operation: pause all writers and
   reconcile every post-backup write first. Schema rollback alone cannot preserve
   new provenance/receipts. Never auto-retry failed general writes via legacy mode.

No deployment, migration, permission/network changes, credentials, health access
or tunnel switch was performed by this change.
