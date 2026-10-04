# Document lifecycle as people see it

How a document's journey from upload to "Ready for Ask" is presented, what the presentation is
derived from, and what it refuses to claim. The ingestion state machine itself is described in
`ingestion.md`; deletion in ADR-026 and `retention.md`.

## Source of truth

`GET /api/v1/documents/{id}/lifecycle` (`document:read`) is computed entirely from persisted state
— the latest version's ingestion job, its stage events, and the parse, chunk, embedding, index and
lexical runs — by `app/services/lifecycle.py`. Nothing is cached and nothing is held in the
browser: a reload reconstructs exactly the same picture. The document list carries a compact
`lifecycle` summary on each row from the same code.

## Stages

Seventeen internal statuses are grouped into seven stages a person can follow:

| Stage | Internal statuses |
|---|---|
| Upload received | UPLOADED, VALIDATING |
| Reading the document | QUEUED, PARSING, NORMALIZING, ENRICHING |
| Preparing searchable passages | READY_FOR_CHUNKING, CHUNKING, VALIDATING_CHUNKS |
| Creating search representations | READY_FOR_EMBEDDING, EMBEDDING |
| Building the semantic search index | INDEXING, VERIFYING_INDEX |
| Building the keyword search index | READY_FOR_RETRIEVAL, SPARSE_INDEXING, VERIFYING_SPARSE_INDEX |
| Ready for Ask | RETRIEVAL_READY |

Each stage is COMPLETED, RUNNING, PENDING, BLOCKED (stopped at NEEDS_REVIEW), FAILED (FAILED,
QUARANTINED) or CANCELLED. Stage durations come from the stage events of the **current pass**: a
rechunk, reparse or re-embed after a stop starts a new pass, and stages after the restart point are
reset rather than shown as done from the earlier attempt.

Overall states: PROCESSING, REVIEW_REQUIRED, FAILED, CANCELLED, READY, ARCHIVED, DELETING,
DELETION_INCOMPLETE, NOT_STARTED. `terminal` is true for every state except PROCESSING.

## What is never shown

- **No percentage and no progress bar.** Stages differ by orders of magnitude (reading a scanned
  book can take an hour, the keyword index a second), so "4 of 7" would say nothing true.
- **No stage advanced by a timer.** The browser polls every 3 s while `terminal` is false and stops
  as soon as it is true. The only thing that moves locally is the elapsed clock, counted from the
  server's `server_time` and the run's own timestamps so a skewed browser clock cannot distort it.
- **No invented completion time** (below).
- **No misleading totals.** A finished or stopped document shows *Processing time*: the sum of its
  recorded stage durations. The span of the last run would understate it after a rechunk (seconds,
  against minutes of reading), and the span since upload would count idle days as processing. A
  running clock that counts from a restart is labelled *Elapsed in this run*.

## Time remaining

An estimate is offered only for *Reading* (seconds per page) and *Creating search representations*
(seconds per passage), from completed runs on this server — parse runs of at least 5 pages that
succeeded or were accepted on review, embedding runs of at least 20 passages. It is shown only when
at least **8** comparable runs exist, as the interquartile range of their rates times this
document's size minus time already spent, labelled "Estimated". Otherwise the page says why:

- fewer than 8 samples → "Time remaining is not available yet … (n of 8)";
- the run has outlasted the slow end of the range → "taking longer than similar documents usually do";
- other stages → "Completion time depends on the document's size and structure."

At the time of writing this server has 4 qualifying parse runs, so no estimate is shown. Better
estimates would need telemetry the pipeline does not yet persist: per-page parse progress events,
an OCR pre-scan (scanned pages dominate parse time), embedding batch progress written during the
run rather than at its end, and a machine identifier on each run so rates from different hardware
are not pooled.

Liveness is reported separately from the estimate: "Worker active — last signal Ns ago" from the
running parse's heartbeat (the only stage that persists one), and after two minutes of silence a
neutral note rather than an alarm.

## Review required

When a job stops at NEEDS_REVIEW, the lifecycle carries a `review`: which stage (PARSE or CHUNK),
the run, blocking and warning counts, findings grouped by code and severity with a few page/chunk
samples, how many earlier attempts with the **same** chunker version, policy fingerprint, result
and blocking codes stopped the same way, and the retry budget.

The interface leads with *why processing stopped* and the blocking issues, open; warnings are one
folded group. Guidance is keyed on code **and** severity — `CHUNK_OVERSIZED` is a blocking ERROR
when a passage exceeds the embedding token budget and a harmless WARNING when an indivisible unit
merely exceeds its target. The first step offered is always to inspect the exact chunk or page.

`actions` lists each reprocessing path with whether it can succeed from here. For a chunk-stage
review, re-embed and lexical reindex are `NOT_APPLICABLE` — the chunk dataset failed validation and
must never be embedded. For a parse-stage review, rechunk and re-embed are `NOT_APPLICABLE`. An
exhausted retry budget marks every applicable path that would spend a retry `NO_RETRIES_LEFT`. The endpoint also drops actions the
caller lacks permission for, so a reader sees the findings and no controls. Nothing in this flow
accepts or bypasses a chunk finding; there is no acceptance path for a chunk dataset.

When identical earlier attempts exist, the page says that repeating the step will most likely give
the same result and spend a retry — unless the chunker has changed since the run under review
(`reviewed_chunker_version` vs `current_chunker_version`), in which case it says so and recommends
rechunking. (Example: "Head and Neck: Muscle Charts" stopped on a 396-token table part against a
384-token budget on two attempts with chunker 1.0.0; chunker 1.1.0 fixes the table splitter,
ADR-027, so rechunking is the recommended remedy.)

### Remediation guidance

Each reprocessing action is explained before it is used — what it does, when to use it, its pros
and cons — in one shared component used by the review panel and the Chunk Inspector:

| Action | Use when | Note |
|---|---|---|
| Rechunk | extraction correct, passages wrong | recommended for chunk-construction findings; new passage IDs; a deterministic defect recurs if nothing changed |
| Reparse | extraction itself wrong (OCR, missing text, malformed table) | slower; may change extraction; does not fix a size problem in a correct extraction |
| Retry | a transient failure | not for a validation result — unchanged input gives the same result |
| Re-embed | valid active passages need new embeddings | never offered for passages that failed validation |
| Rebuild keyword index | valid passages, broken keyword index | never offered for passages that failed validation |

The recommended action is the first remedy the finding's guidance lists among those the server
allows, and none is recommended when an identical attempt already failed with unchanged code.
Unavailable actions are listed with the reason rather than offered as buttons. Passages are never
accepted as they are. Readers receive no actions from the server and see an explanation only.

## Library

Each row shows one chip — *Processing · stage · elapsed*, *Review required · N blocking*, *Ready*,
*Processing failed*, *Archived* — with a link to the document's lifecycle. The list polls only while
some row is processing or deleting. Archive and *Delete permanently* live in the row's ⋯ menu,
the latter only for holders of `document:delete`.

## Document details

Order: overview → processing lifecycle → failure or review → versions (findings and technical
detail folded per version) → ingestion diagnostics (folded) → danger zone (admins only).
