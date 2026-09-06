# Data model

Twenty-four normalized PostgreSQL tables with UUID primary keys and timezone-aware timestamps:
nine from M1, seven added by M2 and eight added by M3. Alembic is the sole schema migration
mechanism; application startup does not call create_all.

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
Searchable is constrained to false. Version page count is null until a parse run succeeds, at
which point it is set from the parsed page count; structural PDF validation on upload is not a
page/provenance extraction pipeline.

Stage events have a per-job sequence with a unique constraint. Timestamp ordering alone is
insufficient because PostgreSQL timestamps can tie within the upload transaction. State transitions
hold the job lock (or create a new unpublished job), allocate sequence and flush each edge.
Database triggers prohibit history/audit mutation and illegal job transitions or provenance changes.

Migrations: `0dd8e0dcb035` creates M1 tables, indexes, constraints and guards;
`m1_event_order` adds/backfills deterministic event sequence; `m2_document_parsing` adds the
parsing tables and the M2 state vocabulary and guard; `m3_hierarchical_chunks` adds the chunk
tables, the M3 state vocabulary and guard, and the outbox message kind. `m1_event_order` briefly disables the
append-only trigger for its controlled backfill. All four have downgrade paths, exercised as
upgrade/downgrade/upgrade in isolated test schemas; downgrading the initial revision removes all
data. Back up before any deliberate application schema downgrade.

## M2 parsing tables

| Table | Implemented responsibility |
|---|---|
| parse_runs | One durable parse attempt: parser name/provider/version, policy version and content fingerprint, frozen config snapshot, source checksum and pinned object version, raw artifact location, counters, OCR mode/engine, validation result, lease and safe error |
| document_pages | 1-based page number, size, rotation, rolled-up normalized text, element count, source text-layer size, OCR indicator and evidence, optional preview object |
| document_elements | Type, parent, sibling ordinal, document-wide reading order, depth, raw and normalized text, TOPLEFT-origin bbox, parser reference and label, content layer |
| table_artifacts | Canonical JSONB cells, row/column/header counts, caption relation, continuation candidacy and evidence, malformed flag, markdown/HTML renderings |
| figure_artifacts | Caption relation, stored crop key and pinned version, dimensions, media type, page and bbox |
| formula_artifacts | Source expression, whitespace-normalized expression, notation, adjacent explanatory paragraph links |
| parse_validation_findings | Append-only severity, code, safe message and details, scoped to document, page or element |

Page numbers are 1-based, enforced by `page_number >= 1`; table `row`/`column` are 0-based grid
coordinates and are never page numbers. Bounding boxes are PDF points with a TOPLEFT origin and
carry their origin explicitly; an unlocated element leaves all box columns NULL. `reading_order` is
unique per parse run. A partial unique index plus a check constraint enforce at most one active,
successful parse run per version. Findings inherit the append-only trigger used by stage events and
audit rows. Parse rows cascade from their run; nothing cascades from a document version's original.

Migration `m2_document_parsing` adds these tables, widens the job/version status vocabulary with
READY_FOR_CHUNKING and replaces the M1 transition guard with the M2 graph including the explicit
reparse edge. Its downgrade drops the parse tables, restores the M1 guard and vocabulary, and
refuses to run while any row still holds an M2-only status rather than silently rewriting it.

## M3 chunk tables

| Table | Implemented responsibility |
|---|---|
| chunk_runs | One durable chunk attempt: source parse run, generation, chunker name/version, policy version and content fingerprint, frozen config snapshot, pinned tokenizer name/revision/runtime, input fingerprint, validation result, metrics, lease and safe error |
| chunks | Type, sequence, parent, question, faithful source text, retrieval representation, both token counts, page range, content hash and structured metadata |
| chunk_source_elements | Ordered element spans with start/end offsets and role (SOURCE, CAPTION, RELATED_CONTEXT, LIST_HEADING, HIERARCHY) |
| chunk_source_pages | Page membership for a chunk, within the same parse run |
| chunk_artifact_relations | Exactly one of table/figure/formula artifact per row, within the same parse run |
| chunk_relations | Ordered sibling relations inside one chunk run |
| question_artifacts | Question text/type/number, explicit source answer, explanation, extraction status, explicit-vs-inferred structure flags, authority metadata and page range |
| question_options | Label, ordinal and text, unique per question on both label and ordinal |

Declared in `backend/app/models/chunking.py`. Composite foreign keys keep every mapping inside the
same tenant, document version, parse run and chunk run: a chunk cannot reference an element from a
different parse run, and a parent cannot live in a different chunk run. A partial unique index plus
a check constraint allow at most one active dataset per version, and only a `SUCCEEDED` run with a
passing validation result may be active. A second partial unique index allows at most one pending or
running chunk run per version. `never_infer_answers` makes an inferred answer unstorable;
`chunk_page_range`, `chunk_tokens_nonnegative`, `source_offsets_valid`, `no_self_parent`,
`chunk_relation_not_self` and `exactly_one_source_artifact` hold the remaining shape invariants.

Triggers enforce the rest: a chunk run must begin pending and inactive with a valid, active,
successful source parse; its identity columns are immutable; a completed run may only have
`is_active` changed; rows of a run that is not `RUNNING` cannot be inserted, updated or deleted, so
historical datasets are immutable; source offsets must resolve against the element's normalized
text; and deactivating a parse run or cancelling a job automatically deactivates the chunk datasets
built from it.

## Future schema (not implemented)

ChunkElement and ChunkRelation will retain ordered parent/neighbor/source joins onto the
document_elements above. Embedding/index versions and activation manifests will keep incompatible
representations isolated.

Answers must snapshot corpus/config/provider/prompt versions, evidence IDs and verification outcomes.
Citation resolution must use immutable stored provenance, never reconstructed current metadata.
Identity membership/role administration, clinical evaluation runs, retention/purge records and
versioned policy registries remain future work. Archival currently preserves originals and audit;
it is not a deletion or retention policy.
