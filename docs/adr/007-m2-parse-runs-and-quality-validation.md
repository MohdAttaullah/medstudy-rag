# ADR-007: Versioned parse runs, derived-artifact storage and fail-closed parse validation

Status: Accepted (M2)

## Context

ADR-001 chose Docling and required that structured output, reading order, page/box provenance and
original artifacts be preserved. M1 delivered a durable upload control plane that stops at
`QUEUED`. M2 has to execute a long, model-backed, non-transactional stage that writes to both
object storage and PostgreSQL, can partially fail, and whose output quality is not implied by the
parser exiting zero.

Three decisions were forced.

## Decision 1 — the parse attempt is a first-class versioned entity

Parsing is recorded as a `ParseRun` carrying the source checksum and pinned object version, the
parser name/provider/version, the policy version and a SHA-256 fingerprint of the whole frozen
policy, the raw artifact location, counters, the validation result and a safe error. A run is
never overwritten. At most one run per version may be `is_active`, enforced by a partial unique
index plus a check that an active run must have succeeded.

*Why.* A parser upgrade or a threshold change produces a different normalized dataset from the
same bytes. Overwriting would silently invalidate every downstream chunk, citation and answer
derived from the previous parse, with no way to compare or to explain the change. Keying
idempotency on a content fingerprint rather than a version label means an edited threshold cannot
quietly reuse a parse produced under different rules.

*Cost.* Superseded runs retain rows and objects; retention is future work. Reparse must be an
explicit, permissioned action (`ingestion:reparse`) rather than an automatic consequence of
configuration drift.

*Rejected.* Mutating a single parse per version (cheap, but unauditable and unreproducible), and
keying reuse on the policy version string alone (defeated by an unversioned edit).

## Decision 2 — the raw parser artifact lives in object storage, not PostgreSQL

The complete Docling export is written to
`documents/<doc>/<version>/parsing/<run>/docling.json`, wrapped in an envelope recording the
parser identity, the configuration actually used and the source page count. Binary page and
picture rasters are stripped from that JSON and stored as separate immutable objects under the
same run prefix.

*Why.* The raw structure must survive re-normalization, parser comparison and bug reproduction
without reparsing, and it is far too large and too rarely queried to belong in relational storage.
Keeping base64 rasters inside the JSON would multiply artifact size for pixels already stored
once. The relational tables hold the parser-independent projection that the domain queries.

*Cost.* Two stores must agree. The pipeline therefore writes the artifact before persisting rows,
pins its object version on the run, and marks a run active only in the transaction that completes
it, so a crash leaves diagnostic rows rather than an authoritative-looking partial dataset.

*Rejected.* A JSONB column (query cost and row size), and discarding the raw output after
normalization (irreproducible, and no way to re-derive structure after a normalizer fix).

## Decision 3 — a successful parser run is not a valid parse

A deterministic rule layer runs after normalization and produces persisted findings plus one
structured result: `PASS`, `PASS_WITH_WARNINGS`, `NEEDS_REVIEW` or `FAIL`. `NEEDS_REVIEW` and
`FAIL` are real terminal job states; only the first two reach `READY_FOR_CHUNKING`.

*Why.* Docling exits successfully on a scan whose OCR produced noise, on a page whose text layer
was lost, and on a table whose cells do not fit its declared grid. Accepting those as evidence
would push undetected content loss into retrieval, where it becomes an unsupported medical claim
with a citation that looks valid. Thresholds are typed, frozen and snapshotted per run; findings
carry codes, counts and page numbers, never document text, and no rule reports an accuracy figure.

*Cost.* Thresholds are judgement calls that need re-tuning on a real corpus, and a `NEEDS_REVIEW`
document needs an operator. At the time of this decision there was no review-approval workflow and
the operator path was reparse or cancel; ADR-018 later added reviewed acceptance, which admits a
flagged parse without rewriting this verdict or removing any finding.

*Rejected.* Trusting the parser's exit status, and asking a language model whether the text "looks
right" (which would make an unverifiable model judgement the gate on evidence quality).

## Consequences

The database, not only the application, enforces the M2 stage graph, the single active dataset and
the append-only findings log. Parsing is attributable to an exact parser build and policy.
Suspicious output is visible instead of silently downstream.

Layout classification proved not to be bit-identical across CPU/BLAS environments: cell contents,
grid geometry, page counts, text and reading order were stable between the Windows host and the
Linux worker container, but semantic labels on borderline regions were not. Consequently the
automated test suite asserts our own persistence and provenance, while the gold extraction-fidelity
dataset is explicitly baselined per environment. Claiming environment-independent structural
determinism would have been false.
