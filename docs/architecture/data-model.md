# Data model

M1 uses nine normalized PostgreSQL tables with UUID primary keys and timezone-aware timestamps.
Alembic is the sole schema migration mechanism; application startup does not call create_all.

| Table | Implemented responsibility |
|---|---|
| tenants | Authorization scope and short upload reservation lock |
| users | Configured identity provenance, tenant and role |
| documents | Logical publication: title, description, strict source/authority, publisher, subject, specialty, creator and archive time |
| document_versions | Immutable file/edition identity, version number/year, filenames, MIME, size, SHA-256, private S3 key/version, uploader and ingestion status |
| ingestion_jobs | Version, requester, current stage/status, config version/snapshot, correlation, timestamps, bounded retry generation and safe errors |
| ingestion_stage_events | Append-only ordered state history, from/to, service, retry, correlation and safe error fields |
| audit_events | Append-only tenant/actor/action/resource/correlation and safe metadata |
| upload_intents | Durable idempotency reservation, fingerprint/hash, proposed IDs/key, expiry, completion/failure and cleanup state |
| outbox_messages | Unique job/generation dispatch, stable message ID, publish attempts/errors and receipt |

Actual SQL names are declared in `backend/app/models/documents.py`. Composite foreign keys enforce
tenant consistency for publication/version/job identity. Constraints include tenant/checksum
uniqueness, publication/version-number uniqueness and outbox job/generation uniqueness.
Version original identity cannot be updated through the immutable-original guard.
Searchable is constrained to false in M1. Page count remains null; structural PDF validation is
not a page/provenance extraction pipeline.

Stage events have a per-job sequence with a unique constraint. Timestamp ordering alone is
insufficient because PostgreSQL timestamps can tie within the upload transaction. State transitions
hold the job lock (or create a new unpublished job), allocate sequence and flush each edge.
Database triggers prohibit history/audit mutation and illegal job transitions or provenance changes.

Migrations: `0dd8e0dcb035` creates M1 tables, indexes, constraints and guards;
`m1_event_order` adds/backfills deterministic event sequence. The latter briefly disables the
append-only trigger for its controlled backfill. Both have downgrade paths; downgrading the initial
revision removes M1 data and is only exercised in isolated test schemas. Back up before any deliberate
application schema downgrade.

## Future schema (not implemented)

DocumentPage/DocumentElement will preserve source/version/page/box provenance. ChunkElement and
ChunkRelation will retain ordered parent/neighbor/source joins. Tables, figures and formulas reference
original elements and generated metadata separately. Embedding/index versions and activation
manifests will keep incompatible representations isolated.

Answers must snapshot corpus/config/provider/prompt versions, evidence IDs and verification outcomes.
Citation resolution must use immutable stored provenance, never reconstructed current metadata.
Identity membership/role administration, clinical evaluation runs, retention/purge records and
versioned policy registries remain future work. Archival currently preserves originals and audit;
it is not a deletion or retention policy.
