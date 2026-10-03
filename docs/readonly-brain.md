# Filtered read-only MCP option

`aeon_readonly` is independently deployable without enabling shared writes.
Its publisher opens the canonical store in SQLite read-only/query-only mode,
selects exact source categories, then screens every row and atomically publishes
a projection. The reader opens only that projection in read-only mode.

Scope: active link/note records in learning, work and side_projects, from the
exact research, bookmark and GitHub sources in `adapter.SOURCES`. Health/inbox,
manual/session-derived records, profiles, transcripts and other sources are
excluded before screening. Preserve original IDs, sources, timestamps and
revisions. `screened_at` means automatic screening, not human endorsement.

Source categories are not privacy classifications. GitHub/bookmark material may
contain private project information. Keyword, secret, email, URL and instruction
screening can produce false negatives and false positives. Approve the category
scope deliberately and treat all retrieved content as untrusted data.

The MCP tools are `aeon_search`, `aeon_recent` and `aeon_get`. Search uses bounded
literal AND matching rather than semantic similarity. Limits include 20 results,
200 candidates, eight query terms, a 500 ms query budget, 250 ms lock timeout,
64 KiB requests, 12,000-character fetches and a 15-minute freshness cutoff.
The publisher allows 30 seconds, 100,000 candidates and 100 MB of text. It retains
the prior projection if publication fails. A five-minute timer is provided.

Example service templates assume root-managed Hermes and consumer account/group
`dot`; adapt and review them for your host. Install root-owned code at
`/opt/aeon-readonly`, projection directory `/var/lib/aeon-dot` root:dot 0750,
and projection root:dot 0640. The publisher gets read-only source/WAL/SHM mounts
and write access only to its output. The consumer gets no raw Hermes paths or
credentials and cannot write the projection.

Reuse an independently authorized private MCP transport. Configure its stdio
command as:

```text
/usr/bin/python3 /opt/aeon-readonly/adapter.py --db /var/lib/aeon-dot/approved-kb.sqlite
```

Keep the transport executable, loaded code and configuration root-owned and
non-writable by its consumer. Retain existing credential isolation; never place
runtime keys, tunnel identifiers or live configuration in this repository.
Grant the consumer namespace only the adapter/output read-only mounts. Verify
actual namespace denial of raw files and projection writes before exposing tools.
Back up units/transport configuration and restore them if checks fail.

For shared writes, use the separate explicit migration and narrowly scoped broker
in [shared-brain-deployment.md](shared-brain-deployment.md). Do not write into
this projection or treat it as another canonical store.

The standalone installation must include the complete `aeon_readonly/projection`
package alongside `adapter.py`, `publish.py` and `projection_schema.sql`. The
research entrypoints select the research policy explicitly; they do not gain
chat-note visibility when the same core is used by the shared-note transport.
