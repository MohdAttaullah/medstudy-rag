# Vector index (M4)

The index holds the dense vectors produced by [embedding](embeddings.md) and makes a verified,
versioned corpus available for a later retrieval milestone. **A verified index is not an answerable
corpus.** No query path, ranking, reranking or generation exists, and every document version stays
database-constrained unsearchable.

## Abstraction

`backend/app/vectorindex/model.py` defines the `VectorIndex` protocol; `qdrant.py` is the only
module that imports `qdrant_client`. Services never call the vendor client directly, so the engine
is replaceable and no orchestration code can come to depend on Qdrant specifics.

The client is pinned to the deployed server's minor version (`qdrant-client >=1.15,<1.16` against
`qdrant/qdrant:v1.15.4`): the client refuses a gap greater than one minor, and the wire format is
only guaranteed within it. Bump the pin and the compose image together.

## Collection design

One shared collection per vector space, named
`medrag_chunks_<schema version>_<semantics fingerprint prefix>`. The vector semantics are part of
the name, so an incompatible model, pooling or dimension change builds a *new* collection beside
the existing one instead of corrupting it.

Vectors are **named** from the start — `medcpt_dense` — so BM25 sparse and late-interaction vectors
can be added to the same points in later milestones without changing point identity. M4 populates
only the dense vector.

`ensure_schema` refuses to adapt: an existing collection whose vector name, dimension or distance
disagrees raises `VECTOR_SCHEMA_MISMATCH` rather than writing into it.

## Point identity

One point per retrieval-eligible chunk, per vector name:

```
point_id = uuid5(fixed namespace, f"{chunk_id}/{vector_name}")
```

Derived from application identity, deterministic on every host and every run, and never generated
by the index. A replayed batch therefore overwrites rather than duplicating, and the mapping
`Chunk ↔ ChunkEmbedding ↔ point` stays one to one and reconcilable in both directions.

## Payload

Routing, filtering and provenance only:

`tenant_id`, `document_id`, `document_version_id`, `chunk_run_id`, `embedding_run_id`,
`embedding_version_id`, `index_run_id`, `chunk_id`, `parent_chunk_id`, `question_id`,
`chunk_type`, `source_type`, `authority_level`, `subject`, `specialty`, `page_start`, `page_end`,
`sequence_number`, `chunk_hash`.

**Chunk text is deliberately absent.** PostgreSQL is the canonical store for source content and a
later retrieval stage resolves chunk ids there anyway, so copying licensed medical bodies into a
second system would widen the blast radius of any index exposure for no retrieval benefit.

`source_type` and `authority_level` are carried so assessment material stays distinguishable from
reference evidence at retrieval time.

## Tenancy

One shared collection with explicit tenant partitioning, not a collection per user. `tenant_id` is
created as a keyword payload index marked tenant-oriented, which lets Qdrant group a tenant's
points on disk, and every server-side query filters it. Tenant isolation is proven at the
repository and service boundary by integration tests, never by frontend filtering, and Qdrant is
never reachable from a browser.

## Payload indexes

Created before any significant load, and only where a filter is actually expected:

| Field | Why |
|---|---|
| `tenant_id` | every retrieval query is tenant scoped; also the tenant partition key |
| `document_id`, `document_version_id` | scope retrieval or reconciliation to one document or version |
| `chunk_type` | filter or weight tables, formulas and questions differently |
| `source_type`, `authority_level` | separate assessment material from reference evidence |
| `embedding_run_id` | reconcile and clean up exactly one run's points |

Nothing else is indexed. An unnecessary payload index costs memory and write throughput.

## Staging, verification and activation

```
EmbeddingRun succeeds
  → IndexRun STAGING          rows recorded in PostgreSQL as the expected set
  → batched upserts           deterministic point ids, so a replay overwrites
  → IndexRun VERIFYING
  → reconciliation            count, identity, vector name, dimension, checksum, payload, tenant
  → IndexRun VERIFIED
  → activation                a PostgreSQL transaction, then the read alias follows
```

Reconciliation reads the points back and compares them against the expected set recorded in
PostgreSQL, producing `IndexValidationFinding` rows. It checks: every expected point present, no
unexpected point in the run, the vector present under the configured name, the right dimension, the
vector checksum byte-identical to what was computed, the tenant correct, and the critical
provenance payload keys correct. Anything less than full agreement fails the run.

## Consistency model, stated plainly

Qdrant gives per-operation acknowledgement, **not multi-operation transactions**. A batch of
upserts is not atomic across batches and this code never pretends otherwise. Correctness rests on
three properties instead:

1. **Deterministic point identity** — a replayed or duplicated batch overwrites rather than
   duplicating.
2. **Read-back reconciliation** against a durable expected set.
3. **Activation as a PostgreSQL state change** performed only after reconciliation. PostgreSQL
   holds the authoritative active-index pointer; a later retrieval milestone reads that pointer,
   not the alias.

Consequences that follow, and are tested:

* A failed replacement **leaves the previous active index untouched**. The old collection is never
  deleted first, and activation of the new run simply never happens.
* Partial points from a failed load remain in the collection tagged with a failed
  `embedding_run_id`. They are never active, never reconciled and never visible to a query scoped
  to an active run.
* Database constraints enforce the rest: `active_index_run_verified` allows `is_active` only for a
  `VERIFIED` run whose verified count equals its expected count, a partial unique index allows one
  active index run per version, and a trigger refuses to activate a run whose embedding run did not
  succeed cleanly.
* Superseding a chunk dataset — a rechunk or reparse — automatically deactivates the embedding and
  index runs built from it, so a stale index can never remain the active corpus for a version.

## Aliases

`medrag_chunks_active` is maintained as a convenience pointer for operators and for a future
model migration: a replacement collection is built alongside the current one and the alias is
switched atomically once it verifies. The alias is **not** the source of truth for what may be
queried; the PostgreSQL `IndexRun.is_active` pointer is. An alias update that fails is logged and
tolerated precisely because it is not authoritative.

## Security

Qdrant is internal infrastructure. Port 6333 is published only for local development and must not
be exposed publicly. The browser never talks to Qdrant: the single live-statistics endpoint is
server-side, tenant-scoped and requires `document:manage`. No provider secret is ever written into
a payload, and no route returns a dense vector.

## Not in M4

Query embedding, vector search, filtering at query time, BM25 or sparse vectors, late interaction,
fusion, reranking and answering. The sparse and multi-vector names are reserved by the named-vector
design but are not created.
