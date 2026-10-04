# Ingestion architecture

How this pipeline is presented to people — stages, review guidance, honest timing — is in
`document-lifecycle.md`. Permanent deletion of a document is ADR-026.

## Implemented boundary

Authorized PDF uploads create an immutable original, DocumentVersion, IngestionJob, ordered stage
history and a durable outbox message. Successful jobs execute
`UPLOADED -> VALIDATING -> QUEUED -> PARSING -> NORMALIZING -> ENRICHING -> READY_FOR_CHUNKING ->
CHUNKING -> VALIDATING_CHUNKS -> READY_FOR_EMBEDDING -> EMBEDDING -> INDEXING -> VERIFYING_INDEX ->
READY_FOR_RETRIEVAL`. Celery confirms receipt in PostgreSQL and then runs the parse, chunk and
embedding pipelines in turn for the job it claimed.
Every version stays unsearchable; a database constraint prevents accidental activation.
READY_FOR_RETRIEVAL means the vectors were built and the index reconciled — it does **not** mean
the document is answerable. Parsing is documented in [document parsing](document-parsing.md),
chunking in [document chunking](document-chunking.md), and embedding and indexing in
[embeddings](embeddings.md) and [vector index](vector-index.md).

`backend/app/ingestion/state.py` owns transitions and writes job/version status, timestamps, history
and audit together. PostgreSQL triggers reject invalid job transitions and changes to immutable job
provenance. The executable graph is:

| Origin | Allowed destination |
|---|---|
| UPLOADED | VALIDATING, CANCELLED |
| VALIDATING | QUEUED, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| QUEUED | PARSING, FAILED, QUARANTINED, CANCELLED |
| PARSING | NORMALIZING, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| NORMALIZING | ENRICHING, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| ENRICHING | READY_FOR_CHUNKING, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| READY_FOR_CHUNKING | CHUNKING, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED; explicit reparse to VALIDATING |
| CHUNKING | VALIDATING_CHUNKS, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| VALIDATING_CHUNKS | READY_FOR_EMBEDDING, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| READY_FOR_EMBEDDING | EMBEDDING, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED; explicit reparse or rechunk |
| EMBEDDING | INDEXING, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| INDEXING | VERIFYING_INDEX, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| VERIFYING_INDEX | READY_FOR_RETRIEVAL, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| READY_FOR_RETRIEVAL | CANCELLED; explicit reparse, rechunk or re-embed |
| FAILED | CANCELLED; explicit bounded retry, reparse, rechunk or re-embed |
| QUARANTINED | CANCELLED |
| NEEDS_REVIEW | CANCELLED; explicit reparse; curator acceptance to READY_FOR_CHUNKING (parse-stage only, consumes no retry); rechunk or re-embed only when a later stage raised the review |
| CANCELLED | None |

READY exists in the enum for schema compatibility and is absent from both the application table and
the database guard: it is reserved for a version that is genuinely answerable, which needs query
retrieval, grounding and verification. Retry only accepts FAILED, and routes a chunk-stage failure
to a forced rechunk rather than a full reparse. Reparse (`ingestion:reparse`) returns a job to
VALIDATING; rechunk (`ingestion:rechunk`) to READY_FOR_CHUNKING; re-embed (`ingestion:reembed`) to
READY_FOR_EMBEDDING. Each consumes one unit of the same bounded retry budget so reprocessing cannot
loop unbounded. Both increment the retry generation, preserve history, check the original's stored
size/checksum metadata and emit a new outbox message on success. Unavailable storage fails the job;
mismatch quarantines it. Neither replaces the original. Cancel is idempotent; archive cancels
eligible jobs and prevents new versions. Document/job locks serialize these actions with receipt
and with an in-flight parse.

## Upload and storage lifecycle

1. Authenticate and require upload permission before reading file bytes. Check existing publication
   tenant scope and archival state before receiving a new version.
2. Validate metadata, UUID request key, filename/extension, MIME and declared size. Stream to a
   temporary disk file while computing SHA-256 and enforcing actual bytes/time limits, even for
   chunked uploads. Check PDF signature, EOF and basic structure in a time-bounded subprocess.
3. In a short transaction, lock the tenant reservation row; check request replay and tenant-local
   duplicate/pending hashes. Persist a PENDING UploadIntent containing its immutable UUID object key.
4. Lock that intent while uploading to private versioned S3 storage. Verify returned size/hash
   metadata and require an object version ID. Under the publication lock allocate its version number.
5. Atomically commit publication/version/job, all three stage events, audit, outbox and COMPLETE
   intent. The validation events record accepted validation; invalid files instead produce rejection
   audits without creating a publication or job.
6. On failure, resolve the intent in a fresh transaction before compensating. COMPLETE means the
   original transaction committed: return its existing IDs and preserve the source. Otherwise mark
   FAILED and delete only the uncommitted intent's key, object versions and incomplete multipart
   uploads. Failed cleanup remains durable as cleanup_required.
7. The dispatcher reconciles expired PENDING or cleanup-required FAILED intents with SKIP LOCKED.
   Active uploads retain their intent lock. Committed version references are never swept. Database
   unavailability defers resolution; the durable intent permits later recovery.

The S3 adapter uses bounded multipart transfer. Original filenames are display metadata; object keys
are `documents/<document UUID>/<version UUID>/original/source.pdf`. Reads pin the stored S3 version ID
and pass through authenticated API streaming. Archival preserves originals; no public purge API exists.

## Idempotency and duplicates

Request keys are scoped by tenant and authenticated actor. The canonical fingerprint includes SHA-256,
version/publication metadata and target publication. Same key/fingerprint after commit returns the
same document/version/job IDs with replayed=true; altered payload returns IDEMPOTENCY_CONFLICT.
Pending attempts return UPLOAD_IN_PROGRESS; terminal failures require a new key. Confirmed failed
responses include retry_with_new_key; the UI preserves keys for ambiguous network failures.

SHA-256 uniqueness is tenant-wide, including archived versions. An exact duplicate returns
UPLOAD_DUPLICATE with only same-tenant resource IDs. Different bytes can create a new edition under
the same publication. There is no fuzzy matching or cross-tenant hash disclosure.

## Queue durability and configuration

The transactionally written outbox is authoritative. The dispatcher publishes stable message IDs
and retries until durable receipt, including after broker failure or lost delivery. A message's
job/generation uniqueness plus document/job/outbox locking makes duplicate or stale receipt harmless.
Receipt checks source metadata and records queue_received_at and audit. Cancellation/archival cannot
be undone by a late task. Queue failure leaves committed jobs QUEUED for redelivery.

Each job freezes the full IngestionConfig snapshot and version, including limits, duplicate policy
and retry cap. Current delivery/recovery tuning controls the dispatcher; historical job snapshots
remain unchanged. Receipt is not a processing lease: `receive` returns the job id for exactly one
delivery, and the parse pipeline then takes its own lease by moving that job out of QUEUED under a
row lock. The lease is recorded on the ParseRun with a worker identity, heartbeat and expiry; the
dispatcher releases expired leases so a crashed worker leaves a retryable job.

## Implemented parsing (M2)

Docling output is preserved as an immutable raw artifact per parse run, and projected into
parser-independent pages, elements, tables, figures and formulas with 1-based page numbers,
TOPLEFT-origin point coordinates, parser-declared hierarchy and deterministic reading order. Table
headers and cells, formula expressions and question-bank cues are retained; nothing is inferred.
A deterministic quality layer decides between READY_FOR_CHUNKING, NEEDS_REVIEW and FAILED.
See [document parsing](document-parsing.md) for the full contract.

A job reaches `NEEDS_REVIEW` from three different stages and only `last_error_code` says which.
`PARSE_NEEDS_REVIEW` means there is no active parse dataset, so rechunk and re-embed cannot
succeed for it — they fail with `CHUNK_SOURCE_PARSE_NOT_READY` after spending one unit of the
bounded retry budget. The way forward for a flagged parse is curator review: see
[document parsing](document-parsing.md) and ADR-018.

## Implemented chunking (M3)

The active parse run is projected into a durable, versioned chunk dataset:
`READY_FOR_CHUNKING -> CHUNKING -> VALIDATING_CHUNKS -> READY_FOR_EMBEDDING`. Work is dispatched
through the same transactional outbox with a distinct `CHUNKING` message kind and taken under a
fenced chunk-run lease, so duplicate delivery is idempotent and a crashed worker leaves a retryable
job rather than a stuck one. Parent and child chunks, row-grouped table parts with repeated headers,
atomic formulas, figure context, and question objects with their options and explicit source answers
are persisted with complete span-level provenance. Chunks, mappings, questions, relations, findings
and active-run selection commit in one transaction after a deterministic quality layer decides
between READY_FOR_EMBEDDING, NEEDS_REVIEW and FAILED. Completed runs are immutable and remain
inspectable. See [document chunking](document-chunking.md) for the full contract.

READY_FOR_EMBEDDING means chunked and validated, not retrievable: no embedding, vector or index
exists, and every version stays database-constrained unsearchable.

## Implemented embedding and indexing (M4)

The retrieval-eligible chunks of the active chunk dataset are embedded with a pinned MedCPT Article
Encoder revision, loaded into a named dense vector in Qdrant, reconciled point for point against
the expected set recorded in PostgreSQL, and only then activated. Work is dispatched through the
same transactional outbox with an `EMBEDDING` message kind and taken under a fenced lease.

PostgreSQL owns the authoritative active-index pointer; Qdrant alone cannot create a cross-system
transaction, so activation is a database state change that happens after read-back verification
rather than a side effect of writing points. A failed replacement leaves the previous active index
untouched, and superseding a chunk dataset deactivates the index built from it.

## Future retrieval and answering (not implemented)

Query encoding, dense and sparse retrieval, fusion, reranking, context expansion, evidence
sufficiency, grounded generation, claim verification and citation validation are later milestones.
READY remains unreachable in both the application graph and the database guard, and Ask stays
disabled.

See [ADR-006](../adr/006-m1-durable-upload-control-plane.md),
[ADR-007](../adr/007-m2-parse-runs-and-quality-validation.md),
[ADR-008](../adr/008-m3-hierarchical-chunking.md) and
[ADR-009](../adr/009-m4-medcpt-embeddings-and-vector-index.md).
