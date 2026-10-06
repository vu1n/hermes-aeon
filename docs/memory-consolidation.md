# Manual proposal-only consolidation

This slice adds `consolidate_preview` and `consolidate_stage` to the existing
agent-neutral gateway and opt-in general MCP surface. No scheduler, model calls,
new schema, production principal, migration or access change is installed.

An authorized client selects currently eligible general source IDs and revisions
from search/recent, then manually invokes:

```python
from brain_service.client import Client
brain = Client('/run/aeon-general/general.sock')
refs = [{'memory_id': '<selected-general-id>', 'revision': 1}]
preview = brain.call('consolidate_preview', {'source_refs': refs})
# Inspect preview['result']['candidate'] before staging.
staged = brain.call('consolidate_stage', {
    'source_refs': refs,
    'expected_candidate_id': preview['result']['candidate_id'],
})
```

Preview requires read capability. Staging requires existing read, derive and
capture capabilities and produces only a `derived_view` with kind `idea`,
client-asserted verification and `owner_review_required` in its structured content.
Staging is not owner approval. No flag or client text can attest human review.
The staging actor is recorded by the canonical writer; source actors, source
class, verification, applicability and expiry remain attached to each claim.

The deterministic baseline groups exact whitespace-normalized source text into
bounded excerpts. It does not infer facts, detect semantic contradictions,
compress ideas with a model or rank confidence. Explicit `contradicts` links
remain unresolved and retain pinned claim/target IDs and revisions. Different
actors, repeated claims and even distinct imported records do not prove external
independence. Source observations are labelled separately from client claims;
there is no count-to-confidence or preference promotion rule.

Derived summaries are traversed to their original eligible sources. Their text
is not additional support. Inferred assertions with links retain those links and
remain client claims. Cycles, uncertified legacy capsules/TOM, restricted/health,
expired, withdrawn or stale-version inputs fail closed. Exact duplicate claims
are grouped without deleting raw records. Canonical sorted claims and original
pins determine the candidate hash; selecting another repeated derived summary
of the same originals produces the same candidate. Deduplication is per staging
principal through the existing receipt ledger, not semantic equivalence or a
cross-principal global uniqueness guarantee.

Staging recomputes the preview and compares its hash, then uses the existing
capture transaction and pinned-reference publication checks. A source update
between preview and capture prevents publication. An identical retry returns the
original durable receipt; always `get` its ID before presenting it, since receipts
are historical and source changes can invalidate the view. Existing broad derived
invalidation remains conservative: unrelated general revisions can invalidate a
candidate too. A fresh capture alone does not invalidate existing candidates;
a revision to an unrelated existing general record does. Identically restaging
an invalidated candidate returns its old idempotent receipt and does not revive
its live eligibility. This baseline requires manual/operator invocation and
provides no regeneration or durable review queue. Do not report an old receipt as
a fresh live candidate; check `get` first. All reads/search and generated exports pass live general policy;
there is no raw-record export or private-data fallback.

One invocation covers one domain, at most 16 selected/expanded unique sources,
20,000 source-content characters, 500 characters per claim excerpt and 16,000
serialized candidate characters. Sources are read with one shared 4,096-unit
lineage budget. Monotonic deadline checks bound orchestration to two seconds at
checkpoints; they do not interrupt an already executing SQLite call. The server's
existing DB/socket deadlines still apply. Character bounds are conservative
input/output bounds, not measured model tokens. Oversized/deep/slow work returns
unavailable without partial candidate output. Clients must split selected source
sets explicitly; this is not automatic whole-corpus processing.

## Owner review and correction boundary

An owner reviews candidate claims, conflicts, provenance and applicability
against originals. There is deliberately no apply operation in this slice.
For a correction, use the existing cross-agent `propose` operation to attach a
separate contradicting claim to the target revision. The original creator can
subsequently use `revise` with expected revision after a separately recorded owner
decision. These APIs do not attest who reviewed the decision. Never treat the
candidate as a verified preference or apply it merely because it was staged.

## Evaluation and canary plan (not enabled)

Maintain a small owner-reviewed synthetic/general-only set covering: independent
source observations, duplicate texts, repeated summaries, explicit conflicting
claims, active context with distinct applicability, withdrawals/version changes,
unknown legacy and health-derived exclusions. Evaluate the exact preview against
originals: coverage, faithful attribution, explicit conflicts, no confidence
inflation, source pin correctness and unchanged raw data. The baseline tests
verify mechanics; they do not establish real-world summary quality.

A later model adapter requires separate design/approval, bounded inputs and
outputs, a timeout/cancellation contract, source-faithfulness and contradiction
evaluations, and explicit permission for model calls/data disclosure. Mock quality
scores cannot substitute for owner-reviewed live evaluation.

After separate rollout approval, canary with a small manually selected general
set and a principal already authorized to derive. Inspect every candidate before
using it, verify live source pins immediately before a correction, track denied/
stale/oversized results and raw-record counts. Do not enable scheduling or
automatic apply during the canary. Roll back by stopping invocations and retracting
owned staged candidates through the existing API; raw sources remain intact.
Revoking/reconfiguring access or restoring a backup requires separate approval.
See [general-service-rollout.md](general-service-rollout.md) for unchanged socket,
backup, migration and filesystem boundaries.

For explicit durable attempts, metadata-only pending/decision history and fresh
linked regeneration, see [consolidation-review.md](consolidation-review.md). The
original consolidation operations above keep their receipt semantics unchanged.
