# Changelog

## M5 - 2026-09-06

- Added query-side retrieval with the MedCPT Query Encoder pinned by revision and by both weight
  and tokenizer checksum, reproducing the released representation: CLS pooling, 768 dimensions,
  unnormalized, 64-token maximum, inner-product similarity against the M4 article vectors.
- Added a versioned biomedical BM25 lane in PostgreSQL whose analyzer keeps identifiers intact —
  HLA-B27, CYP3A4, Na+/K+-ATPase, HbA1c, IL-6, mg/kg, 7.5% — with stopwords disabled and no
  synonym or abbreviation expansion, enforced by a database constraint.
- Stored raw term frequencies and document lengths rather than pre-weighted scores, so BM25 k1 and
  b are runtime-safe and inverse document frequency is scoped to the tenant's own active corpus.
- Added reciprocal rank fusion with deterministic ties, and DENSE_ONLY, BM25_ONLY and HYBRID_RRF
  modes. Raw lane scores are carried as diagnostics and never added together.
- Made dense/lexical corpus alignment fail closed: a version is searchable only when both lanes are
  active, verified and built from the same chunk dataset.
- Added a durable sparse-index lifecycle with reconciliation against the dense lane's own recorded
  chunk set, activation only after verification, and preservation of the previous active index when
  a replacement fails.
- Rejected over-long queries with a structured error instead of truncating them, and kept query
  text out of logs, traces and the bounded query-vector cache.
- Moved interactive query encoding into its own internal service with an offline provisioned model
  cache, so the API and dispatcher images carry no torch and no request downloads a model.
- Added a retrieval API, sparse-index inspection, an explicit lexical rebuild, and a Retrieval
  Inspector UI with lane comparison, execution trace and candidate to source-page navigation.
- Added a gold retrieval dataset and an evaluation harness reporting Recall@K, MRR, nDCG, precision,
  per-category results, per-query diagnostics, failure classification, negative-case score
  separation and query-boundary behaviour, with the dataset hash recorded in every report.
- Measured, and reported as measurements: the fused RRF score does not separate answerable from
  unanswerable queries and must not be used as an evidence-sufficiency signal.
- Advanced the pipeline to RETRIEVAL_READY. Ask remains disabled, READY remains unreachable, every
  version remains database-constrained unsearchable, and every retrieval response states
  answering_enabled: false. No reranking, expansion, grounding or generation exists.

## M4 - 2026-09-06

- Added document-side embeddings with the MedCPT Article Encoder pinned by revision and weight
  checksum, reproducing the released representation: CLS pooling, 768 dimensions, unnormalized,
  512-token maximum, inner-product similarity.
- Added a provider-independent embedding abstraction; only one adapter imports transformers or
  torch, and only one imports the vector-database client.
- Added deterministic two-field embedding inputs built solely from persisted chunk content and
  declared hierarchy, with a SHA-256 hash over exactly what the model sees.
- Made truncation impossible to apply silently: an over-long chunk fails the run with a finding
  naming it, and a truncated vector is unstorable by database constraint.
- Excluded parent chunks from first-stage retrieval by policy, and kept question-bank material
  distinguishable through payload authority metadata.
- Added durable EmbeddingVersion, EmbeddingRun, ChunkEmbedding, IndexRun and IndexValidationFinding
  records, with one active embedding run and one active index run per version enforced by the
  database.
- Added a Qdrant index with named dense vectors, deterministic uuid5 point identity, provenance-only
  payloads, tenant-oriented payload indexing and server-side tenant scoping.
- Added staged indexing with point-for-point read-back reconciliation, and activation as a
  PostgreSQL state change that happens only after verification, so a failed replacement leaves the
  previous active index untouched.
- Made superseding a chunk dataset automatically deactivate the embeddings and index built from it.
- Executed READY_FOR_EMBEDDING -> EMBEDDING -> INDEXING -> VERIFYING_INDEX -> READY_FOR_RETRIEVAL in
  the worker with idempotent delivery, cancellation, lease sweeping and an explicit permissioned
  re-embed.
- Added offline model provisioning with checksum verification and an `embedding-models` volume, so
  no user request depends on a runtime model download.
- Added tenant-authorized embedding and index inspection APIs, an embedding summary on document
  details, an Index Inspector, and the real index stages in Operations. No dense vector is exposed.
- Added the offline embedding technical-quality evaluation harness and a host/container numerical
  comparison tool.
- Indexing stops at READY_FOR_RETRIEVAL. Query retrieval, reranking and answering stay disabled,
  and READY remains unreachable.

## M3 - 2026-09-06

- Added structure-aware hierarchical chunking over the active parse run: parent and child chunks,
  row-grouped table parts with identical repeated headers, atomic formulas, figure context chunks
  and atomic question objects with their options.
- Added a durable versioned ChunkRun with chunker/policy identity, a policy fingerprint, a separate
  normalized-input fingerprint, a fenced lease and a single active dataset enforced by the database.
- Bundled the MedCPT WordPiece tokenizer with its revision, file checksum and runtime pinned, used
  only to measure and slice tokens offline; a mismatch fails closed.
- Added complete span-level provenance: every chunk resolves to source elements, offsets, pages,
  artifacts and the active parse run, and the UI links back to the exact source page.
- Preserved explicit source answers and left absent answers absent; an inferred answer is
  unstorable by database constraint.
- Added a deterministic chunk-quality layer with persisted findings, PASS/PASS_WITH_WARNINGS/
  NEEDS_REVIEW/FAIL, and source coverage measured by text rather than by element identity.
- Executed READY_FOR_CHUNKING -> CHUNKING -> VALIDATING_CHUNKS -> READY_FOR_EMBEDDING in the worker
  with idempotent delivery, cancellation, lease sweeping and an explicit permissioned rechunk.
- Made completed chunk datasets immutable and made superseding a parse run or cancelling a job
  deactivate the datasets built from it.
- Added tenant-authorized chunk inspection APIs, a chunk summary on document details, a Chunk
  Inspector with question and table views, and the real chunk stages in Operations.
- Added the offline chunk construction evaluation harness over 14 synthetic normalized fixtures.
- Chunking stops at READY_FOR_EMBEDDING. Embeddings, indexing, retrieval and answering stay
  disabled, and their job states remain unreachable.

## M2 - 2026-09-06

- Added Docling parsing behind a parser-independent abstraction; only one adapter imports Docling.
- Added durable versioned ParseRun with parser/policy identity, content fingerprint, source
  checksum, lease and a single active dataset enforced by the database.
- Persisted the raw parser artifact, page previews and figure crops in private object storage.
- Added parser-independent pages, elements, tables, figures and formulas with 1-based page
  numbers, TOPLEFT point coordinates, parser-declared hierarchy and deterministic reading order.
- Added deterministic normalization that repairs extraction artefacts and never rewrites content.
- Added OCR configuration with recorded engine, per-page OCR indicator and suspicious-OCR review.
- Added a deterministic parse-quality layer with persisted findings and PASS/PASS_WITH_WARNINGS/
  NEEDS_REVIEW/FAIL, plus fail-closed parse errors with declared retryability.
- Executed QUEUED -> PARSING -> NORMALIZING -> ENRICHING -> READY_FOR_CHUNKING in the worker, with
  idempotent delivery, cancellation handling, lease reaping and an explicit reparse action.
- Added tenant-authorized parse inspection APIs, a parse summary on document details, a parse
  inspector with page previews and table/figure/formula views, and real stages in Operations.
- Added the parsing extraction-fidelity gold dataset and evaluation harness.
- Parsing stops at READY_FOR_CHUNKING. Chunking, embeddings, retrieval and answering stay disabled.

## M1 - 2026-09-05

- Added development authentication and server-enforced tenant/role permissions.
- Added PDF upload validation, immutable versioned originals, metadata, SHA-256 duplicates,
  durable upload-intent recovery and transactional ingestion outbox.
- Added PostgreSQL migrations, guarded job transitions and append-only ordered history/audit.
- Added real Celery receipt, bounded retries/cancel/archive, config snapshots and safe telemetry.
- Connected Library uploads/details and Operations to persisted APIs with real transfer progress.
- Added synthetic fixtures, failure/race/security tests and a live upload/worker browser flow.
- Successful processing stops at QUEUED. Medical answering and M2 processing remain disabled.

## 0.1.0 — 2026-09-05

- Added M0 monorepo foundation, original agent instructions and synchronized project skills.
- Documented target architecture with five ADRs and explicitly synthetic evaluation fixtures.
- Added typed configuration, FastAPI liveness/readiness, correlation IDs, safe request logging and
  request metrics; SQLAlchemy/Alembic and provider/Celery interface foundations.
- Added React workspace routes with disabled medical answering and live Operations health.
- Added Compose definitions, optional app/worker containers, dependency locks and verification commands.
- No domain ingestion, retrieval, generation, RBAC or production deployment is implemented in M0.
