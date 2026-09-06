# AI HANDOFF

## Current milestone and status

M4 - Biomedical embedding generation, versioned vector representations, Qdrant indexing, verified
index activation and embedding/index-quality validation: COMPLETE and verified locally.
M0, M1, M2 and M3 remain verified.

Successful jobs execute
UPLOADED -> VALIDATING -> QUEUED -> PARSING -> NORMALIZING -> ENRICHING -> READY_FOR_CHUNKING ->
CHUNKING -> VALIDATING_CHUNKS -> READY_FOR_EMBEDDING -> EMBEDDING -> INDEXING -> VERIFYING_INDEX ->
READY_FOR_RETRIEVAL.

READY_FOR_RETRIEVAL means the vectors were built and the index reconciled point for point, so this
corpus version is technically eligible for a later retrieval milestone. **It does not mean the
document is medically answerable.** No query encoder, search, ranking, reranking or generation
exists; every version stays database-constrained unsearchable, Ask is disabled and READY is
unreachable. **M5 has not started.**

## Completed implementation (M4)

- MedCPT Article Encoder pinned by revision `d05a736da4bb84ee4057b7f7999485be6ed85465` and weight
  SHA-256 `a5d5ffe4…845b0f3`, verified before load. The released representation is reproduced
  exactly: [CLS] last hidden state, 768 dimensions, unnormalized, 512-token maximum, DOT similarity.
  The pinned tokenizer is byte-identical to the one M3 already uses for chunk measurement.
- Provider independence: only `embeddings/medcpt.py` imports transformers or torch, and only
  `vectorindex/qdrant.py` imports qdrant-client. Services depend on the `EmbeddingModel` and
  `VectorIndex` protocols.
- Deterministic two-field embedding input built solely from persisted chunk content and declared
  hierarchy, hashed over exactly what the model sees. No LLM, no summaries, no invented text.
- Truncation is REJECT and cannot be silent: an over-long chunk fails the run with a CRITICAL
  finding naming it, embeds nothing, and a truncated vector is unstorable by check constraint.
- Explicit retrieval eligibility. TEXT_PARENT is excluded and cannot be enabled by configuration.
  Question-bank chunks are indexed but keep source type, authority level and question status.
- Durable EmbeddingVersion (keyed on a semantics fingerprint, so batching changes reuse the vector
  space while representation changes create a new one), EmbeddingRun, ChunkEmbedding, IndexRun and
  IndexValidationFinding. Dense arrays live in Qdrant; PostgreSQL holds the auditable expected set.
- Qdrant collection named for the schema version and semantics fingerprint, with a **named** dense
  vector `medcpt_dense` so sparse and late-interaction vectors can join the same points later.
  Deterministic uuid5 point identity. Payload carries routing and provenance only, never chunk text.
- Shared collection with a tenant-oriented indexed `tenant_id` payload key and server-side scoping;
  cross-tenant isolation proven at the repository boundary, never in the frontend.
- Staged indexing with point-for-point read-back reconciliation (count, identity, vector name,
  dimension, checksum, payload, tenant), then activation as a PostgreSQL state change. A failed
  replacement leaves the previous active index untouched; superseding a chunk dataset deactivates
  the embedding and index runs built from it.
- Worker pipeline over the existing outbox with an EMBEDDING message kind: idempotent delivery,
  cancellation, fenced leases, dispatcher sweeping, declared retryability, vector reuse on exact
  input-hash match, and an explicit permissioned re-embed.
- Offline model operation: `scripts/provision_embedding_model.py` provisions the pinned revision
  into the `embedding-models` volume with checksum verification, and the worker runs with downloads
  disabled. No user request depends on a runtime model download.
- Tenant-authorized inspection APIs, an embedding summary on document details, an Index Inspector
  with point -> chunk -> source page navigation, real index stages in Operations, and DB-derived
  index metrics. No dense vector is exposed by any route or view.
- Offline embedding technical-quality evaluation harness, a host/container numerical comparison
  tool and a throughput benchmark.

Modules: backend/app/{core/embedding_config,embeddings,vectorindex,models/embeddings,
repositories/embeddings,schemas/embeddings,api/embeddings,services/embedding,evaluation/embeddings};
workers/{celery_app,dispatcher}; frontend/src/features/embedding;
migrations/versions/m4_embeddings_and_index; scripts/{provision_embedding_model,evaluate_embeddings,
compare_embedding_environments,benchmark_embeddings,smoke_m4}.
See [M4 report](docs/verification/m4.md), [embeddings](docs/architecture/embeddings.md),
[vector index](docs/architecture/vector-index.md) and
[ADR-009](docs/adr/009-m4-medcpt-embeddings-and-vector-index.md).

## Git and files

Branch: main. Commits: **6cca864** ("feat: complete M3 deterministic medical chunking"),
**0d2085a** (M2) and **bb2bfa0** (M1).

All M4 work is **uncommitted** in the working tree — 40 modified tracked files and 27 untracked
paths; nothing is staged. No history was rewritten, reset or force-pushed, and no prior agent's
work was discarded. `.env`, `.local` (which now also
holds the provisioned model cache and the environment-comparison output), caches, dependencies,
build outputs and browser artifacts remain ignored; no secret and no model weight is in the tree.
**The milestone commit decision is the user's.**

Host-account Git commands may still need the scoped override because sandbox identity initialized
Git: `git -c safe.directory=D:/Projects/RAG/Proj_1_DoctorDocuments status --short --branch`.

## Verification executed

| Check | Final result |
|---|---|
| MEDRAG_RUN_INTEGRATION=1 pytest -q (M3 baseline, before changes) | 203 passed |
| MEDRAG_RUN_INTEGRATION=1 pytest -q (final) | 277 passed (22 upstream warnings) |
| pytest backend/tests/test_m4_units.py -q | 49 passed |
| MEDRAG_RUN_INTEGRATION=1 pytest backend/tests/test_m4_integration.py -q | 22 passed |
| ruff check / ruff format --check backend workers scripts | Passed, 141 files |
| mypy backend/app | Passed, 104 source files |
| python scripts/check_skills.py | 9 synchronized pairs passed |
| alembic current / alembic check | m4_embeddings_and_index (head) / no drift |
| alembic downgrade m3_hierarchical_chunks -> upgrade head -> check | Passed after the prescribed cancel |
| npm --prefix frontend test | 36 passed (5 files) |
| npm --prefix frontend run build | TypeScript and Vite passed |
| MEDRAG_E2E_LIVE=1 PLAYWRIGHT_CHANNEL=chrome playwright test | 8 passed |
| docker compose config --quiet / up -d --build --wait | Passed / all services healthy |
| scripts/provision_embedding_model.py into the embedding-models volume | PASS, checksum verified |
| scripts/smoke_m1.py / smoke_m2.py / smoke_m3.py / smoke_m4.py | PASS / PASS / PASS / PASS |
| scripts/evaluate_embeddings.py | 9/9 cases, 180/180 checks |
| scripts/evaluate_chunking.py | 14/14 cases, 94/94 checks |
| scripts/evaluate_parsing.py (host) | 9/9 cases, 142/142 checks |

Two stale boundary assertions were updated for M4 reality (the liveness milestone string, and the
unreachable-state test, which now asserts the M5 boundary). One Playwright flake was fixed at its
cause: the default 30 s test timeout was tight for a cold Vite compile of the larger bundle, so the
config now sets 60 s. No test was disabled or weakened.

## Migrations and infrastructure

Applied: `0dd8e0dcb035`, `m1_event_order`, `m2_document_parsing`, `m3_hierarchical_chunks`,
`m4_embeddings_and_index` (head). The M4 revision adds five embedding and index tables with their
constraints and guards, adds READY_FOR_RETRIEVAL to the status vocabulary (no column widening is
needed, and none is possible: triggers depend on that column), replaces the transition guard with
the M4 graph including the re-embed edge, and adds the embedding outbox reference. Its downgrade
refuses to run while any row still holds an M4-only status; cancel those jobs first, as the guard's
message says.

Running: API 127.0.0.1:8000; frontend 127.0.0.1:5173; PostgreSQL 5432; Redis 6379; MinIO 9000/9001;
Qdrant 6333/6334 (now holding real collections); worker and outbox dispatcher. The worker builds
from `infrastructure/docker/worker.Dockerfile` with both the `parsing` and `embedding` extras and is
~2.8 GB. Two model caches, provisioned differently:

* `parser-models` -> `/home/medrag/.cache` — Docling weights, downloaded on first use.
* `embedding-models` -> `/home/medrag/models/embeddings` — the pinned MedCPT revision, provisioned
  deliberately; the worker then runs with `MEDRAG_EMBEDDING__OFFLINE=true`.

To provision or re-provision the embedding cache:

```
docker run --rm --user 10001 \
  -v medical-rag_embedding-models:/home/medrag/models/embeddings \
  -v <repo>/scripts:/repo/scripts:ro -v <repo>/backend:/repo/backend:ro -w /repo \
  medical-rag-worker:latest python /repo/scripts/provision_embedding_model.py \
  --cache /home/medrag/models/embeddings
```

`scripts/` and `backend/tests/` are not copied into the image; bind-mount the repository to run an
evaluation or benchmark inside it. Generated credentials remain in ignored `.env`; UI keys are in
`.local/dev-access.txt` — do not print them, and note that `MEDRAG_DEV_PRINCIPALS` is single-quoted
so the quotes must be stripped when exporting it to Playwright.

## Decisions, limitations and blockers

ADR-001 through ADR-008 remain accepted. **ADR-009** records the MedCPT choice, CLS pooling, 768
dimensions, DOT similarity, named vectors, deterministic point identity, index versioning and
activation, and the tenant partition strategy.

**Determinism: three separate claims, measured separately.**

1. Same host, same batch layout, repeated inference — **byte identical**. Asserted.
2. Batched versus single inference on one host — numerically equivalent, **not** byte identical
   (padding changes the last bits). Asserted with a tolerance, never as equality.
3. Host versus canonical Linux worker, 11 fixed inputs — **0/11 byte identical**, worst max
   absolute delta 9.537e-07, cosine agreement 1.000000000, and 11/11 ranking agreement. Measured
   and reported; deliberately not asserted as equality anywhere.

`vector_checksum` is therefore a byte identity for **storage integrity** — it proves the index
returned what was computed — and never a claim of cross-machine reproducibility or of embedding
quality. Vector reuse keys on `input_hash` and the embedding version, never on a checksum. The
canonical stored corpus embeddings are the ones produced by the Linux worker.

Other limitations: **retrieval quality is entirely unmeasured** and will remain so until a query
encoder and a gold retrieval set exist; the similarity sanity check is two anecdotal pairs and
nothing is tuned against it; CPU inference runs at ~1.9 embeddings/second with ~1.2 GB peak worker
memory, with no GPU, quantization or ONNX path; resumability is per-run rather than mid-batch;
orphan points from a failed load are never active but are not garbage-collected; no retention or
cleanup of superseded runs or collections; per-batch embedding duration is recorded in run metrics
but is not scrapeable; `verify_sample_ratio` exists but full verification is always used; Qdrant is
a single unauthenticated local node with no TLS, API key, replication or backup. Parsing and
chunking limitations from M2 and M3 are unchanged, including the measured parser-label variation
between CPU environments. This remains local development and is not clinical validation or
compliance certification. Production startup remains explicitly rejected.

No remaining M4 blocker.

## Next exact task

Stop. Wait for the user's M5 authorization. Then begin:
**M5 - Biomedical hybrid retrieval: MedCPT Query Encoder + dense search + BM25/sparse retrieval +
reciprocal-rank fusion + retrieval-quality evaluation.**

Read AGENTS.md, this handoff, [embeddings](docs/architecture/embeddings.md),
[vector index](docs/architecture/vector-index.md), [retrieval](docs/architecture/retrieval.md),
ADR-002/003/009 and the retrieval-quality and rag-evaluation skills. Query only the **active**
IndexRun (`is_active`, `VERIFIED`) resolved from PostgreSQL, never the alias and never an
unverified run; every query must carry the tenant filter server-side. The query encoder is
`ncbi/MedCPT-Query-Encoder` and must be pinned and provisioned the same way as the article encoder;
it is a different model and needs its own version record. Sparse retrieval adds a second named
vector to the existing points rather than a second collection. Build retrieval evaluation with a
gold query set, separate from parsing, chunk and embedding evaluation, and report Recall@K, MRR and
nDCG only once they are actually measured. Do not enable Ask, generation, grounding or answering.

## Do not do

- Do not expose partially processed versions or enable medical answering.
- Do not present a verified index as an answerable or searchable corpus.
- Do not fabricate parse, chunk, embedding, index or retrieval progress, accuracy percentages or
  clinical accuracy.
- Do not claim cross-machine bit-identical embeddings; the measured deviation is documented above.
- Do not tune the architecture against the two-pair similarity sanity check, or quote it as
  retrieval accuracy.
- Do not promote question keys, captions or generated metadata to authoritative medical evidence.
- Do not infer a question answer the source does not state.
- Do not let a model rewrite, interpret or "correct" parsed source text, values or formulas.
- Do not relax the parsing, chunking or embedding gold datasets to hide a difference; re-baseline
  and report.
- Do not delete named volumes, rotate credentials by editing .env, reset or discard uncommitted
  work, overwrite durable instruction files, or claim checks that were not run.
- Do not commit secrets, model weights, or expose Qdrant or the development stack publicly.

Last updated: 2026-09-06 by Claude Code.
