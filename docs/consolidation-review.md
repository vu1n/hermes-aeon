# Durable consolidation review and regeneration

This follow-up adds review attempts and immutable decisions without changing
broad derived invalidation or the original `consolidate_stage` receipt contract.
It remains manually invoked deterministic exact-text extraction. No model,
scheduler, deployment, production migration or authority binding is enabled.

`review_stage` accepts request_id, source_refs and expected_candidate_id from
`consolidate_preview`; optional parent_attempt links a fresh generation to old
history. The attempt and its new derived candidate are recorded in the canonical
capture transaction, including the durable receipt. Lost responses replay the
same attempt; changed payload with the same request ID conflicts. A distinct
request ID intentionally creates a new pending attempt, even for identical
candidate content. Eligible pending entries flag duplicates with duplicate_group (an opaque
attempt ID) and has_duplicates for matching creator, content, source pins and
parent. Clients may group those entries, but each attempt still needs its own
decision. Regeneration ancestry stays distinct, and decisions never supersede
earlier decisions automatically.

Regeneration with parent_attempt requires the original staging principal and
exactly the same original source identities. The caller supplies freshly selected
current revisions and previews them first. Changed, withdrawn, expired or
restricted sources fail closed. This supports unrelated invalidation and explicit
source-version updates while preventing replacement with unrelated evidence.
Supporting-source set changes require a separate new attempt without claiming
regeneration ancestry. Old receipts, attempt records and decisions are unchanged.
Legacy consolidate_stage remains a compatibility-only, unqueued capture API.
New review clients should use consolidate_preview followed by review_stage.
Legacy retries retain their old receipts and cannot regenerate invalidated
candidates; fetch live eligibility before using a receipt. Legacy candidates are
not silently backfilled into review history. Routing that API into review or
backfilling old candidates requires a separate compatibility change.

## Operations and authority

| Operation | Required authority | Behavior |
| --- | --- | --- |
| review_stage | read, derive, capture | Atomic pending attempt and new candidate |
| review_pending | read, review | Bounded metadata-only pending page, including invalid attempts |
| review_history | read, review | One attempt and its immutable decision, independent of live eligibility |
| review_decide | read, review | One accepted/rejected decision; expected revision required except unavailable closure |

`review` is an explicit trusted server capability. Existing principals do not gain
it. A production operator would separately approve who is allowed to represent
owner review and configure the OS UID binding; no client actor/header/boolean
can supply this authority. The principal is authenticated by the existing local
transport; this does not prove a human physically made the decision. An agent
with derive capability can queue candidates but cannot accept them.

Acceptance checks live candidate eligibility, pinned source versions, the exact
candidate content hash and expected revision while holding BEGIN IMMEDIATE.
Eligible rejection also checks expected revision. To close an unavailable attempt,
omit expected_revision and reject: the transaction rechecks current live
ineligibility before recording a decision. If it has become eligible, closure
fails and the reviewer must fetch it and supply a revision. A revision-free
closure stores expected_revision=0 as an explicit unavailable sentinel; it does
not export or compare a hidden candidate revision. Rejection with a supplied
revision also requires a live candidate; otherwise omit the revision. Acceptance
always requires a revision and never uses this closure path.
Competing reviewers have one transactional winner; a conflicting decision cannot
rewrite it. The same reviewer/request payload replays historical decision metadata.
Historical accepted status never grants permission to read a now-ineligible
candidate. Eligible queue/history entries include candidate_ref={memory_id, revision},
using the latest authorized live get result. A reviewer can take that ref, fetch
content through get, and decide with that revision without a staging receipt.
Unavailable candidates return candidate_ref=null; no hidden ID or current
revision is returned. The ref grants no permission and can become stale; get and
acceptance independently recheck policy. Fetch content only through live get.

Reasons are bounded codes: faithful, conflict, outdated, duplicate or irrelevant.
A rejection for outdated/irrelevant does not label a source semantically false.
No free-text source-derived reason is retained in the metadata history, to avoid
leaking revoked content. Rich reason notes would need their own live eligibility
and source lineage contract in a later slice. History stores opaque attempt links,
staging/review actor, reviewed candidate revision and timestamps; returned history/pending pages contain no
source text, titles, applicability, source pins or content hash. Pending pages
use oldest-first (created_at, attempt_id) ordering with an opaque next_after
cursor containing both values, maximum 50 entries, and a shared live-read work
budget. Equal timestamps use attempt ID as a stable tie-breaker. A decision
between pages does not invalidate the cursor. Duplicate flags use indexed lookups across
pages; unavailable entries expose no duplicate group. Group membership changes as individual attempts receive decisions.
Pagination is live, not a snapshot: restart from the first page to see newly
queued attempts whose ordering key precedes a previously consumed cursor.

Decisions are review metadata only: acceptance does not apply corrections,
attest a verified preference or alter candidate text; rejection does not retract
raw records or automatically change recall eligibility. Candidate content retains
owner_review_required as its generation-time label; query history for the actual
decision. Projecting accepted/rejected status into ordinary reader results is a
separate follow-up; this PR does not rewrite the hash-bound candidate JSON. A creator can separately retract an owned candidate if needed. A new
regeneration always needs a new decision. Original evidence and raw records remain
untouched by this workflow.

## Explicit schema preparation and rollback proposal

The additive brain migration adds brain_review_attempts and
brain_review_decisions. An approved operator would back up under a writer pause,
then rerun `python3 -m brain_service.migrate --db <approved-existing-db>`.
It adds tables and queue/duplicate lookup indexes; existing stage/capture work without review tables, while
review operations fail unavailable until explicit migration. Tests apply schema
only to disposable synthetic databases. No production migration was run.

Roll back clients to PR4 operations and stop review invocations. Leave additive
history and durable writes intact; do not drop live tables or rewrite receipts.
Restore a backup only with separate approval and reconciliation of all subsequent
writes. Review authority provisioning, socket access and canary rollout remain
separately approved operations. See memory-consolidation.md for owner evaluation;
mechanical regressions do not prove real-world summary quality.

## Optional advisory classifiers (future)

A future provider-neutral duplicate/conflict/support/priority classifier may
record rubric/question version, exact input revision hashes, model version and
score distribution, alongside a human reasoned decision. Such advice never
supplies review authority, establishes independent evidence or bypasses live
policy. A rejection is a workflow decision, not necessarily a semantic negative.
No Strands dependency, classifier engine, confidence permission rule or paid
model calls are introduced here.
