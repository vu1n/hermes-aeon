# Shared canonical brain

Hermes and the private MCP endpoint share one canonical database. Hermes and
the local broker load the same `store/shared_writer.py`. The MCP process has
no permission to open that database; its search view is a separately filtered,
read-only projection. Installing this code enables neither services nor access.

## Writes and attribution

Each operation begins an immediate transaction with bounded lock retries. The
consumer/request ID, canonical payload digest and response are persisted in the
same transaction as the item, revisions, FTS, optional existing vector and audit.
An identical retry returns the committed response. Reusing a key for a different
payload returns conflict. Existing dedup keys remain unchanged; concurrent
captures serialize before checking the unique key. Chat notes do not merge merely
because their text resembles another note.

Use an exclusive connection per worker/operation. Commit or roll back any caller
transaction before invoking the shared writer; it deliberately refuses to nest
or roll back unrelated pending changes.

Corrections require `expected_revision`; conflicts never overwrite a newer
revision. Original capture source is preserved, and each revision identifies its
actor, kind, attribution basis and optional opaque conversation reference.
Request IDs should be stable opaque identifiers for a conversation/message/note
event, bounded to 128 characters. Use a new ID for a correction. An uncertain
response must be retried with the same ID and payload.

MCP capture/correction allows concise ideas, decisions, preferences and project
context in work, learning and side_projects, up to 4,000 statement characters.
It excludes health, sensitive finances, secrets, URLs and full transcripts by
default. The source is server-fixed to `chat:dot` from an authenticated Linux
peer UID. Clients cannot claim another actor or feed source. Only this source's
notes can be corrected through the broker. Other endpoint users share this
consumer identity; it is not independently verified human authorship.

Use `assistant_inferred` for interpretations; `user_explicit`/`user_corrected`
are caller-reported bases. Screening is conservative, imperfect and can reject
innocuous text or miss unrecognized sensitive content. Retrieved material must
be treated as source data, never instructions. Existing local Hermes ingestion
retains its broader domains and types; the MCP grant does not expand to them.

## Consistency and refresh

`aeon_get` fetches a current owned note through the broker, checking the same
source/content/domain/type/status/audit filters as the projection. No raw-file
access is granted. It sees a committed capture or correction immediately,
including a Hermes revision of an originally dot-owned note. If current content
is excluded, it returns null rather than an older projected copy. Unavailable
broker reads fail without claiming freshness. Feed records use the projection.

Both writers create one empty persistent post-commit dirty marker. Bursts
coalesce. An inotify-backed systemd path unit wakes the existing projection
publisher, which waits 200 ms and consumes the marker before its read snapshot.
A commit during publication creates another marker, causing a subsequent pass.
Publication remains bounded, filtered, exclusively locked and atomically replaced.
Failure preserves the old view and restores the marker with retry backoff.
The five-minute timer is periodic reconciliation for crashes or missed hints,
not the normal visibility path. Search/recent becomes fresh after publication;
it does not have a strict immediate-read guarantee.

Rebuild cost depends on corpus size and load. Benchmark your approved source
with `benchmark_latency.py`; it opens it read-only and deletes its private
temporary projection afterward. Incremental publishing can be considered if
measured rebuild cost warrants it; the current full snapshot preserves the
established filtering boundary.

See [shared-brain-deployment.md](shared-brain-deployment.md) for permission,
migration, backup, activation and rollback steps.
