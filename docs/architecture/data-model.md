# Data model

Twenty-nine normalized PostgreSQL tables with UUID primary keys and timezone-aware timestamps:
nine from M1, seven added by M2, eight added by M3 and five added by M4. Alembic is the sole schema
migration mechanism; application startup does not call create_all.

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
tables, the M3 state vocabulary and guard, and the outbox message kind;
`m4_embeddings_and_index` adds the embedding and index tables, the M4 state vocabulary and guard,
and the embedding outbox reference. `m1_event_order` briefly disables the
append-only trigger for its controlled backfill. All five have downgrade paths, exercised as
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

## M4 embedding and index tables

| Table | Implemented responsibility |
|---|---|
| embedding_versions | What a vector *means*: provider, model id and revision, tokenizer revision, weight checksum, dimension, pooling, normalization, distance metric, input limit, dtype, input-builder version, config snapshot and the semantics fingerprint that identifies the vector space |
| embedding_runs | One durable embedding attempt over one chunk dataset: eligible/embedded/reused/failed counts, input and policy fingerprints, fenced lease, metrics and safe error |
| chunk_embeddings | Metadata for one vector: point id, vector name, input hash, vector checksum, dimension, token count, norm, truncated and reused flags. The dense array itself lives in the vector index |
| index_runs | One durable index load: physical collection, alias, vector name, schema version, expected/indexed/verified point counts, upsert batches, activation time and safe error |
| index_validation_findings | Append-only severity, code, safe message and details for vector sanity and index reconciliation |

Declared in `backend/app/models/embeddings.py`. An embedding version is unique on its semantics
fingerprint, so a batch-size change reuses the vector space while a pooling or model change creates
a new one. Composite foreign keys keep every vector inside the same tenant, document version, chunk
run and embedding run: a `chunk_embeddings` row must reference a chunk of its own run's dataset, and
an `index_runs` row must reference an embedding run of its own tenant.

Partial unique indexes allow one active and one pending-or-running embedding run per version, and
one active index run per version. `active_embedding_run_complete` allows `is_active` only for a
succeeded run that embedded every eligible chunk with no failures;
`active_index_run_verified` allows it only for a `VERIFIED` run whose indexed and verified counts
both equal its expected count. `chunk_embedding_never_truncated` makes a truncated vector
unstorable, which is the database half of the reject-never-truncate policy.

Triggers enforce the rest: an embedding run must begin pending and inactive against a currently
active, validated chunk dataset; identity columns are immutable; a completed run may only have
`is_active` changed; rows of a run that is not `RUNNING` cannot be inserted, updated or deleted;
an index run may only be activated when it is verified and its embedding run succeeded cleanly; and
deactivating a chunk dataset automatically deactivates the embedding and index runs built from it,
so a stale index can never remain the active corpus for a version.

## M5 retrieval tables

`query_encoder_versions` records what a *query* vector means: model id, revision, tokenizer
revision, both file checksums, dimension, pooling, normalization, metric, dtype, maximum query
tokens and the query-normalization version. It is separate from `embedding_versions` because the
query encoder is a different checkpoint with a different tokenizer and a much shorter input limit.
Rows are keyed by a semantics fingerprint and created on first successful encode, so a report can
name the encoder that produced a measured number.

The lexical lane lives entirely in PostgreSQL rather than as a second Qdrant vector, and that is a
deliberate choice: inverse document frequency is then computed over exactly the corpus a query may
see — one tenant's active, verified, version-aligned indexes — instead of over whatever else shares
a collection, including other tenants and superseded runs. `sparse_index_versions` holds the
analyzer identity alone (case policy, compound policy, stopwords, term-length bounds, expansion),
keyed by an analyzer fingerprint. It deliberately excludes `k1` and `b`: postings store raw term
frequencies and document lengths, so saturation and length normalization are applied at query time
and are runtime-safe.

`sparse_indexes` is the per-document-version build, mirroring `index_runs`: chunk-run lineage,
status, activation, counts, term and posting totals, corpus fingerprint, lease and correlation.
`sparse_documents` holds one row per indexed chunk with its length and the facets the lane filters
on. `sparse_postings` holds `(sparse_index_id, chunk_id, term, term_frequency)` with an index on
`(term, sparse_index_id)` — the lookup every query makes. `sparse_terms` holds the per-index
document and total frequency, so query-time IDF is a cheap sum across the active indexes rather
than an aggregation over postings; document frequencies are additive because a chunk belongs to
exactly one sparse index. `sparse_validation_findings` is the append-only reconciliation audit.

Chunk identity is shared with the dense lane. There is no separate lexical document id, so a
candidate from either lane resolves through the same chunk to the same source element and page.

Partial unique indexes allow one active and one building sparse index per version.
`active_sparse_index_verified` allows `is_active` only for a `VERIFIED` index whose indexed and
verified counts both equal its expected count. Triggers enforce the rest: an index must begin
`STAGING` and inactive; identity columns are immutable; a completed index may only change its
activation state, its activation timestamp is written once, and its only permitted status move is
`VERIFIED` to `SUPERSEDED`; rows of an index that is not `STAGING` cannot be inserted, updated or
deleted; an index may only be activated alongside an active `VERIFIED` dense index run built from
the *same* chunk run; and deactivating a chunk dataset automatically deactivates the sparse index
built from it, exactly as it already deactivates the embedding and index runs.

`outbox_messages` gains a nullable `sparse_index_id` and the `SPARSE_INDEX` kind, which is only
meaningful while a job sits in `READY_FOR_RETRIEVAL`.

The M5 revision is also the one that widens the status columns, because `VERIFYING_SPARSE_INDEX`
is longer than any status before it; see [deployment](deployment.md) for what that requires.

## Future schema (not implemented)

ChunkElement and ChunkRelation will retain ordered parent/neighbor/source joins onto the
document_elements above. Embedding/index versions and activation manifests will keep incompatible
representations isolated.

Answers must snapshot corpus/config/provider/prompt versions, evidence IDs and verification outcomes.
Citation resolution must use immutable stored provenance, never reconstructed current metadata.
Identity membership/role administration, clinical evaluation runs, retention/purge records and
versioned policy registries remain future work. Archival currently preserves originals and audit;
it is not a deletion or retention policy.

## M6 request-scoped evidence

M6 adds typed, fingerprinted model/reranking/expansion/budget contracts, not relational entities.
EvidenceSet and EvidenceBlock resolve existing immutable M2/M3 sources. Interactive queries/evidence
are not persisted. Canonical question artifacts retain explicit keys and assessment authority.
Alembic head remains `m5_hybrid_retrieval`; no empty M6 migration exists. No ingestion state changes,
and `RETRIEVAL_READY` does not mean medical answer readiness.

## M7 request-scoped decisions

M7 adds typed, fingerprinted sufficiency, grounding and provider policies, not relational entities.
The sufficiency decision, the grounded draft and the abstention are request-scoped and none of them
is persisted; neither is the question or the provider response. Alembic head remains
`m5_hybrid_retrieval` and no empty M7 migration exists. Source authority keeps the M1 meaning, where
assessment source types are already forbidden from claiming REFERENCE or HIGH authority, and M7
relies on that rather than restating it.

## M8 request-scoped verification

M8 adds typed, fingerprinted claim-extraction, verification, contradiction, repair and final-policy
contracts, not relational entities. Claims, verdicts, contradiction findings, verified answers and
abstentions are request-scoped and none is persisted. Alembic head remains `m5_hybrid_retrieval` and
no empty M8 migration exists. Source authority keeps its M1 meaning throughout verification.

## M9 conversations

The first new durable state since M1: `conversations`, `conversation_turns` and `turn_citations`,
all tenant-scoped through composite keys. A turn stores the question, the outcome, the answer only
when verified, declared reason codes, model identity and timings; a citation stores the identifiers
that resolve to the live document plus the exact text the answer was verified against. Two CHECK
constraints put the display rule in the schema: `answer_text` exists exactly when the outcome is
VERIFIED, and `verified` agrees with the outcome. Prompts, provider responses, failed drafts,
verifier reasoning and EvidenceSets are not persisted. Alembic head is `m9_conversations`; its
downgrade refuses while history exists unless the loss is explicitly acknowledged.


## M10 configuration

M10 adds append-only `configuration_revisions` (tenant/revision uniqueness and an actor/tenant
foreign key) and a nullable `conversation_turns.configuration_snapshot`. A PostgreSQL trigger
rejects history mutation; a populated downgrade is guarded. See ADR-015.
