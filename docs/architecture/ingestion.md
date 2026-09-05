# Ingestion architecture

## Implemented M1 boundary

Authorized PDF uploads create an immutable original, DocumentVersion, IngestionJob, ordered stage
history and a durable outbox message. Successful jobs execute `UPLOADED -> VALIDATING -> QUEUED`.
Celery confirms receipt in PostgreSQL and leaves the job QUEUED. Every version is unsearchable;
a database constraint prevents accidental activation. No parsing or later processing runs.

`backend/app/ingestion/state.py` owns transitions and writes job/version status, timestamps, history
and audit together. PostgreSQL triggers reject invalid job transitions and changes to immutable job
provenance. The executable graph is:

| Origin | Allowed destination |
|---|---|
| UPLOADED | VALIDATING, CANCELLED |
| VALIDATING | QUEUED, FAILED, QUARANTINED, NEEDS_REVIEW, CANCELLED |
| QUEUED | FAILED, QUARANTINED, CANCELLED |
| FAILED | CANCELLED; explicit bounded retry to VALIDATING |
| QUARANTINED, NEEDS_REVIEW | CANCELLED |
| CANCELLED | None |

Future enum values exist for schema compatibility but are not executable. Retry only accepts FAILED,
increments the retry generation, preserves history, checks the original's stored size/checksum
metadata and emits a new outbox message on success. Unavailable storage fails the job; mismatch
quarantines it. Retry does not replace or reparse the original. Cancel is idempotent; archive cancels
eligible jobs and prevents new versions. Document/job locks serialize these actions with receipt.

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
remain unchanged. Receipt is not a processing lease. Long-running M2 stages will require explicit
leases/fencing and their own failure/retry artifacts.

## Future parsing and activation (not implemented)

Docling output must preserve structured JSON, reading order, heading ancestry, physical page number,
printed labels and bounding boxes with coordinate systems. Preserve table headers/row groups,
formula context and complete question objects. Generated enrichment has separate lineage.

Later indexing uses a fresh staging manifest. Activation requires parse/provenance validation,
complete embeddings, exact expected index IDs/counts, reconciliation and retrieval smoke success.
PostgreSQL owns the atomic manifest switch; Qdrant alone cannot create a cross-system transaction.
Readers pin an authorized READY manifest. Crashed staging writes stay invisible; retention policy
governs cleanup. M1 never performs this activation.

See [ADR-006](../adr/006-m1-durable-upload-control-plane.md).
