# ADR-015: Typed tenant policies and explicit pending rebuild proposals

Status: Accepted for M10. Baseline `7f3b857` (committed M9). Preserve ADRs 010–014.

## Decision

An explicit registry projects approved fields from existing Pydantic policies. It is not a Settings
serializer and never exposes arbitrary environment keys, endpoints, bearer credentials or provider
keys. Code owns section, lifecycle, scope, type, bounds, editability and impact. The frontend only
renders these contracts. Existing literal model/vector pins stay read-only even where their impact
is REINDEX_REQUIRED. No alternate embedding model is invented for a dropdown.

Runtime policies are tenant scoped. Only existing admins receive settings:read/settings:write;
curators and readers cannot administer settings. Global service configuration remains startup-owned
and read-only because the existing tenant admin role is not a system-operator identity.

The approved provider registry is built from startup-approved model pairs plus configured generator
and verifier pairs. Both adapters already exist. The browser cannot add a provider, model, endpoint
or secret. Optional providers can remain absent. An unset verifier retains the M8 generator fallback.

## Revisions and activation

PostgreSQL stores append-only tenant revisions with complete effective allowlisted snapshots,
runtime overrides, pending desired values, actor, time, request id, reason, old/new values and impact.
A tenant-row lock serializes first and subsequent writers. Expected revision rejects stale writes;
a preview digest additionally binds tenant, startup identity and the exact validated change. All
changes require confirmation. Runtime and rebuild mutations cannot be mixed in one batch.

Runtime activation is one committed revision. Each request resolves it once and copies the existing
service graph with frozen policies; in-flight requests keep their snapshot. Model adapters and
immutable caches are shared; policy objects are never mutated globally. Ask persists the snapshot
with its turn, without changing its verified-only response or storing rejected drafts.

Chunk and lexical analyzer changes are tenant rebuild proposals. Their effective values stay at the
startup contract; M10 does not dispatch rebuilds, restart services or mark proposals activated merely
because an environment value later matches. An operator must perform the established versioned
chunk/dense/sparse rebuild, reconciliation and activation outside M10. A future lifecycle reconciliation
feature would need verified manifest identity; desired-value equality is not activation evidence.

## A configuration surface may not tighten the policy it exposes

M10 validates every change against the real policy types, and beyond those it asserts only
constraints the underlying milestones already hold. The temptation is the opposite: a settings
screen invites rules that look reasonable in isolation, and each one quietly becomes a new policy
that no milestone agreed to and no test covers.

One such rule was written and removed. M10 initially refused any state where
`evidence_budget.max_tokens_per_block` exceeded `max_total_tokens`, which reads like an obvious
sanity check. M6 holds no such relation: `EvidenceBudgetConfig` declares no validator between the
two, and assembly applies them independently — a block is refused if it exceeds either — so a total
below the per-block ceiling simply means the total binds first. `test_m6_units.py` builds exactly
that state and asserts the resulting single block and budget finding. The M10 rule would have
rejected a configuration M6 verifies, which is a configuration surface redefining the semantics it
is supposed to expose.

The two cross-field rules that remain are M5's own, not M10's. A reranking candidate pool larger
than `dense_top_k + sparse_top_k` is refused because `RetrievalConfig` declares that invalid and M6
assigns `candidate_top_k` into that field through `model_copy`, which does not re-run validators —
so without the check the invalid state is reachable through configuration rather than merely
unconstructable. Both fusion weights at zero is refused because M6 and Ask always search with
`HYBRID_RRF` regardless of `retrieval.mode`, so it disables retrieval on the path this surface
configures. Each is enforced because a verified milestone holds it, and a regression test now pins
both directions: what M10 must keep rejecting, and what it must not start rejecting.

## Precedence and invariants

Immutable code invariants precede all other sources. Only allowlisted runtime fields may override
startup/environment values; other effective fields remain startup-owned. Defaults apply when startup
values are absent. Historical snapshots retain their exact effective values across later deploys.
A stored runtime policy incompatible with the current registry or typed bounds fails closed.

Source authority remains the existing audited document metadata policy: assessment material never
becomes reference truth. Safety switches, gate skipping, verifier skipping, unverified streaming,
truncation, partial-ingestion search and credential editing are not configurable. The frontend cannot
supply scope, lifecycle, effective state or activation status.

## Migration and rollback

`m10_configuration` revises `m9_conversations`, adds configuration_revisions and a nullable historical
configuration snapshot to conversation turns. Composite actor/tenant foreign keys, positive/unique
revision constraints and a database mutation-rejection trigger protect history. Existing turns remain
valid with no invented historical snapshot. Downgrade refuses while policy history or new turn
snapshots exist unless an operator explicitly acknowledges configuration-history loss. Test schemas
acknowledge loss only for their disposable data. Application rollback must account for active tenant
overrides; returning to M9 would intentionally stop consuming them.

M11 and M12 are outside this decision. This remains the development-authenticated system established
by earlier milestones, not a production security claim.
