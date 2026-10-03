# Shared-brain deployment and rollback

This runbook is an opt-in storage/security change. Publishing or merging code
does not approve deployment. Obtain explicit approval for the canonical handoff,
additive migration, writer service, socket access, private event directory,
screened owned-note reads and private endpoint's capture/correction tools.
The code does not grant privileges or install/start services automatically.

Examples assume Linux, a root-managed Hermes instance, existing consumer
account/group `dot`, and a private MCP transport already authorized independently.
Review the identity, paths and installed backend before using the templates.
The source allowlist currently approves `chat:dot`; additional consumers require
an explicit scope change, not just another UID mapping. External Turso sync is
not certified for this shared local broker: validate it separately before use.

## 1. Back up and pause all writers

Record the current code commit and dirty changes, unit/transport configuration,
database counts, ownership and schedule definitions. Make root-only backups of
code/configuration and a consistent SQLite backup of the canonical database;
verify the backup with integrity_check. Never copy a live database file alone
without its transaction/WAL state. Keep backups outside this repository.

Stop the projection timer/path and pause the Hermes gateway plus already running
or independent ingestion writers. For a root user-manager gateway, use its
actual unit through `XDG_RUNTIME_DIR=/run/user/0 systemctl --user`. Do not confuse
it with a system-manager unit of the same name. Confirm no writer is active;
retain schedule definitions for resumption. Checkpoint/close the source before
handoff. No transcripts, credentials, unrelated databases or home directories
should move or be granted to the broker.

## 2. Keep one canonical store

SQLite requires writable space for WAL/SHM recreation, so do not grant the broker
a mixed credential/transcript directory such as the entire Hermes home. Move
the authoritative store to `/var/lib/aeon-canonical/aeon.db` under the writer
pause. Keep the old entry (for example `/root/.hermes/aeon.db`) as a protected
symlink so scheduled ingesters reach the same store. Never run two writable
copies. Validate the symlink/WAL behavior in a synthetic fixture first.

Create dedicated non-login `aeon-writer`, group `aeon-kb-writers`; grant no
consumer membership in that group. Canonical directory:
`root:aeon-kb-writers` **2770**; database and sidecars **0660**. Setgid preserves
the group for recreated sidecars. Root Hermes continues to access this store;
the dedicated broker can access only this canonical directory and its event
directory. The consumer must still fail direct raw-file reads.

## 3. Install code and apply explicit schema migration

Install the reviewed repository commit as root-owned code in
`/opt/hermes-aeon` (directories 0755, files 0644). Preserve/reapply reviewed
unrelated local changes deliberately; do not overwrite a dirty checkout blindly.
The affected Hermes files are `store/queries.py`, `store/shared_writer.py`,
`store/utils.py`, `provider.py`, `ingest/_common.py` and the memory skill.
All code and ancestor directories must be non-writable by consumers or broker.

After base schema initialization and before any capture/update, explicitly run:

```sh
cd /opt/hermes-aeon
python3 -m aeon_sharedwrites.migrate --db /var/lib/aeon-canonical/aeon.db
```

This additive, transactional migration creates `memory_write_requests`,
`memory_write_audit` and its actor index. It refuses a missing or wrong database,
retains existing records, and may be repeated. It runs neither on import nor
from a request. For a new installation, initialize the ordinary base schema
first (provider initialize does this), stop/pause the provider, then migrate
before issuing captures. Existing Hermes update callers now require the revision
returned by reads; the profile updater passes the revision it fetched and leaves
conflicts visible rather than silently overwriting.

Install the transport-only `aeon_sharedwrites` directory at
`/opt/aeon-sharedwrites`, also root-owned/non-writable. The broker loads the exact
canonical `/opt/hermes-aeon/store/shared_writer.py` using `--shared-module`;
there is no separately maintained writer implementation. The consumer need not
receive access to the Hermes plugin tree.

## 4. Review permissions and service templates

Create `/etc/aeon-brain` root-owned 0700 and `consumer.env` root-owned 0600:

```text
AEON_CONSUMER_UID=<numeric UID verified with id -u dot>
AEON_CONSUMER_ID=dot
```

This file contains only the peer mapping, no API credentials. UID identity is
server-derived with SO_PEERCRED; clients cannot submit an actor/source.

Install the reviewed templates from `aeon_sharedwrites`:

* `aeon-write.socket` / `aeon-write.service` into `/etc/systemd/system`.
* `aeon-write.conf` into `/etc/tmpfiles.d`; create its specified directories.
* `aeon-dot-refresh.path` / `aeon-dot-refresh.service` into `/etc/systemd/system`.
* The existing `aeon_readonly/aeon-dot-refresh.timer` as periodic reconciliation.

`/run/aeon-write`: root:dot **0750**, socket root:dot **0660**.
`/var/lib/aeon-events`: root:aeon-kb-writers **2770**. Only approved writers
create empty hints; the consumer cannot trigger rebuilds directly.
`/var/lib/aeon-dot`: root:dot **0750**, projection root:dot **0640**;
publisher temporary/lock files remain root-only.

The network-isolated broker has a strict read-only filesystem except canonical
and event directories, no home visibility, no capabilities, and four bounded
workers. The root publisher gets read-only canonical/WAL/SHM visibility plus
write access only to event/output directories and CAP_CHOWN for projection
ownership. It retains the established filtered projection boundary.

Keep the existing private transport's credentials, grants and root-owned client
executable unchanged. Expose only read-only mounts for `/opt/aeon-sharedwrites`,
`/var/lib/aeon-dot` and the socket directory. Connecting to a Unix socket does
not require a writable mount. Canonical files, Hermes home, auth and plugin tree
remain hidden from the transport consumer. Configure its stdio command as:

```text
/usr/bin/python3 /opt/aeon-sharedwrites/mcp_adapter.py --db /var/lib/aeon-dot/approved-kb.sqlite --broker-socket /run/aeon-write/broker.sock
```

Initialize the new filtered projection with `read_adapter/publish.py` using the
canonical source, output above, and `--reader-group dot`, before switching the
MCP command. No runtime key, tunnel identifier or live transport config belongs
in the repository.

## 5. Activate and verify before real records

Reload unit definitions, enable the write socket and refresh path/timer, then
resume/reload the gateway so it uses the patched callers. Resume ingestion
cadences without redefining schedules. Switch/reload only the private transport
needed for the new adapter. Verify an explicitly identified synthetic note:

* Discover three reads and capture/correct; no delete/status/raw-query tool.
* Capture and identical retry produce one item/revision; changed-payload key
  reuse returns conflict. A correction appends history; stale revision conflicts.
* Fetch sees the committed owned note immediately. Search/recent updates after
  event publication; current excluded content cannot fall back to an old note.
* Verify peer impersonation and other-source updates are denied, filtering
  excludes sensitive domains/content, and raw DB/auth access and projection
  writes fail inside the actual consumer mount namespace.
* Check gateway startup/ingestion and aggregate counts without printing content.
  Confirm socket activation, dirty-hint recovery and periodic reconciliation.

Do not admit real conversation notes until these checks pass. Broker calls
perform no extraction, embedding or external API access. Existing Hermes calls
retain their configured embedding behavior; tests stub/disable those APIs.

## Rollback

Disable write exposure and stop the socket/path first. Restore the previous
read-only transport command/units and regenerate its old-scope projection from
the current canonical store. Pause writers before restoring Hermes code or
changing the storage entry. Additive ledger/audit tables can remain safely.
After any accepted writes, preserve the latest canonical database: restoring
a stale backup would discard memories. If reversing the path handoff, use a
verified latest SQLite backup/checkpoint and resume exactly one authoritative
store. Restore schedule cadence and verify raw-file denial again. Retain
root-only backups for operator review; never publish them.
