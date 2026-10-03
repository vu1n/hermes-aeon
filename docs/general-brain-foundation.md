# General brain service foundation

This is the bounded, code-only first phase of the [shared-brain proposal](https://github.com/vu1n/dotspace/blob/21aa6cff75d8649bfc2262a15de26aa79701c834/research/2026/10/2026-10-03-shared-brain-architecture.md).
It adds a common general-memory contract over the existing canonical writer.
It does not deploy a service, change access, admit legacy records, or implement
a health corpus. The supported domains are work, learning and side_projects.

## Identity and operations

`brain_service.gateway.Gateway` accepts exactly `api_version`, `operation` and
`arguments`. The API version is `brain.general.v1`. A trusted adapter resolves
the peer to a server-configured `Principal`; the Linux socket adapter obtains
the UID from SO_PEERCRED. Actor, owner, capabilities, source and verification
are not client fields. Only owner `local` is supported in this phase.

| Operation | Required capability | Result or rule |
| --- | --- | --- |
| capture | capture | Assertion or working context; source fixed by principal |
| capture evidence | capture, import | Immutable source observation; new source versions are new records |
| capture derived_view | capture, derive, read | Nonempty pinned derived_from inputs, all currently eligible |
| revise | revise_own | Original creator only, expected revision and reason required |
| retract | retract_own | Original creator only; append a retracted revision |
| propose | propose, capture, read | Separate assertion contradicting the target revision |
| get, search, recent, status, interests | read | Current eligible general records only |

Imported evidence cannot be revised or retracted by general clients. Cross-agent
corrections never overwrite another agent's assertion. Compatibility aliases
`aeon_capture`, `aeon_get`, `aeon_search` and `aeon_recent` use the service shapes;
`aeon_correct` maps the existing statement/summary/kind correction shape into a
typed revision. This gateway is not installed behind the existing MCP adapter.

Request IDs are scoped to principal and payload. Identical retries return the
original receipt even after a later revision; changed-payload reuse conflicts.
CAS, item mutation, complete revision snapshot, attribution, pinned references,
receipt and general index update share the canonical writer transaction.
Capture origin is immutable; editing actor is recorded separately. A supplied
attribution basis or opaque conversation reference is a client claim, not human
authentication, owner attestation or permission to fetch a transcript.

## Recall and interests

Evidence, assertion, working_context and derived_view are explicit record
classes. Topics are bounded normalized labels with a small explicit alias map.
Search and recent support topic, project, domain, source, type and record-class
filters. Search uses a dedicated general-only lexical index, bounded candidates
and per-document matching, with a small active-context relevance boost. It does
not use canonical cross-domain vector candidates or global corpus statistics.
Recent totals cover the bounded candidate window; `total_is_complete` states
whether that window reached its cap.

The index is synchronous in this phase. A write receipt distinguishes durability
from `visible` or `ineligible` search visibility and includes the general commit
sequence. Status reports commit and indexed sequence. A search requesting a
future minimum sequence returns `indexing_pending` rather than claiming fresh
results. Restricted-only writes do not advance the general sequence.

Working context defaults to 14 days and expires at read time. Agent statements
are `client_asserted`; imports are `source_observation`. Neither attribution
labels, inferred topics nor repeated feed views create verified owner preferences.
There is no owner-attestation or automatic promotion operation in this phase.

## Live eligibility and scope

Every result is checked against live owner, sensitivity, status, expiry, current
revision and pinned lineage before output or counting. Stale index rows cannot
restore revoked records. A general input change conservatively invalidates all
stored derived views; recomputation is explicit. Full returned envelopes,
including metadata and provenance, receive the shared restricted-content screen.
Keyword screening is defense in depth, not proof that arbitrary concealed or
inferred health information is absent. Trusted classification and complete
lineage remain necessary. Unknown legacy records are not automatically admitted.
Screening visits decoded string values and object keys throughout the envelope;
JSON escapes cannot change the screening regex's whitespace or word boundaries.
The existing 150,000-character serialized-envelope limit still applies.

Lineage checks memoize shared descendants within each live traversal, keyed by
record, revision and depth. One 4,096-unit work budget is shared across each
service operation's candidate reads and write validation/publication phases.
Node visits and reference edges consume work, including cache hits. Exhaustion
returns `general_unavailable` without partial results; an in-flight write rolls
back atomically. Traversal caches never persist into another read or a later
transaction recheck. The existing depth-eight and per-record reference limits
remain in force.

Hermes search, recent, prefetch, system prompt and digest use the common general
policy. Digest revalidates input revisions after generation and discards a result
if an input changed. Legacy TOM, capsules and derived profiles have no certified
lineage and are excluded. Calendar and old profile/health handlers return an
unavailable error; health/profile tools are not advertised. Existing trusted
Hermes capture/update behavior remains compatible, including canonical storage
of records outside the general scope. Such records are not general recall.

The Hermes host and this store component still have a canonical DB connection.
This implementation does not isolate a compromised host process from canonical
data. Future general clients must receive only a gateway connection, with no
raw canonical files, credentials or host-process execution access. The existing
filtered snapshot MCP and broker retain their separate deployment contract;
publishing this foundation does not grant them these new operations.

## Explicit migration and later deployment

An approved operator would apply the existing shared-writer migration first,
then `python3 -m brain_service.migrate --db <approved-existing-store>` under a
writer pause. The additive migration refuses a missing or wrong database and
does not backfill or classify records. It never runs on import or a request.
This PR runs migrations only on disposable synthetic fixtures.

Before real deployment, separately decide principal bindings and least privilege,
OS isolation, socket lifecycle, backup and rollback, legacy admission and trusted
classification. Quotas/rate controls, cross-feed deduplication, asynchronous or
semantic indexing, owner attestation and health access are deferred. No service
unit, credential, production configuration or migration is supplied by this
foundation. See [testing.md](testing.md) for verification and its limits.

## Security remediation

The two confirmed medium-severity findings were excessive repeated traversal of
shared lineage and screening JSON-escaped text. The bounded traversal and decoded
screening above address them, with synthetic regressions for shared graphs,
depth-sensitive memoization, live revocation, budget exhaustion and rollback,
escaped whitespace, nested metadata, ordinary general text and envelope bounds.
This code-only draft remains subject to the separately approved deployment
decisions described above; these fixes do not install or expose the gateway.
