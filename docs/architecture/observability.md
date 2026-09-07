# Observability

M1 added persisted ingestion history and security/operations audit to M0 HTTP health telemetry;
M2 added parse-stage tracing, parse audit and durable parse metrics; M3 added the same for
chunking, and M4 for embedding and vector indexing.
UUID request IDs are returned as X-Request-ID; only syntactically valid caller IDs are accepted.
The upload ID follows validation/checksum/storage, committed job, outbox and receipt.

Safe structured phase events include UPLOAD_CHECKSUM_COMPLETED, UPLOAD_VALIDATION_COMPLETED,
UPLOAD_STORAGE_VERIFIED, UPLOAD_COMMITTED, INGESTION_QUEUE_PUBLISHED, INGESTION_QUEUE_UNAVAILABLE
and, for M2, PARSE_RUN_STARTED, PARSE_RAW_ARTIFACT_STORED, PARSE_NORMALIZED, PARSE_RUN_COMPLETED
and PARSE_RUN_FAILED. Together they trace the parse path: Celery receipt, source download, parser
initialization, conversion, raw artifact write, normalization, database persistence, artifact
extraction, validation and the state transition. M3 adds CHUNK_RUN_STARTED, then
CHUNK_TABLES_STARTED, CHUNK_FORMULAS_STARTED, CHUNK_FIGURES_STARTED, CHUNK_QUESTIONS_STARTED and
CHUNK_TEXT_STARTED emitted **from the point that work actually begins** - a phase with no source
material is never announced - followed by CHUNK_VALIDATION_STARTED and CHUNK_DATASET_COMMITTED. M4 adds
EMBEDDING_RUN_STARTED, EMBEDDING_INPUTS_BUILT, EMBEDDING_VECTORS_COMPLETE, INDEX_STAGING_STARTED,
INDEX_VERIFICATION_STARTED and INDEX_ACTIVATED, which together trace the embedding path: claim,
input construction, batched inference, staged upsert, read-back reconciliation and activation. Audit emits document/version/job creation, upload
rejection, duplicate detection, every state transition, retry, reparse request, cancel, archive,
metadata change, reconciliation, queue receipt, parse start, parse completion (with result and
counters), parse failure (code only), parse reuse, chunk run creation, chunk reprocess request, chunk
completion (with result and chunk count), chunk failure (code only), chunk cancellation and chunk
dataset reuse, embedding run creation, embedding reprocess request, index run failure, index run
activation (with point count and collection), embedding failure (code only) and embedding
cancellation. Events carry
actor/tenant/resource/correlation and safe metadata in PostgreSQL. Stage history additionally records
sequence, stage, prior/new state, retry, service identity and safe errors. Logs emitted inside a
transaction are diagnostics; committed PostgreSQL records remain the source of operational truth.

| Metric | Meaning |
|---|---|
| uploads_total | Authenticated upload requests |
| uploads_rejected_total | Rejected authenticated requests |
| upload_bytes_total | Bytes accepted by committed new uploads |
| ingestion_jobs_created_total | Jobs created by this API process |
| ingestion_jobs_failed_total | Persisted transitions into FAILED |
| ingestion_jobs_by_status | Current persisted jobs, labeled by status |
| storage_operation_failures_total | Storage failures observed by upload/compensation service |
| duplicate_uploads_total | Duplicate requests detected by this API process |
| parse_runs_by_status | Persisted parse runs, labeled RUNNING/SUCCEEDED/FAILED/CANCELLED |
| parse_runs_by_result | Persisted validation results, labeled PASS/PASS_WITH_WARNINGS/NEEDS_REVIEW/FAIL |
| parse_validation_findings | Persisted findings, labeled by severity |
| parse_pages_total, parse_tables_total, parse_figures_total, parse_formulas_total, parse_ocr_pages_total | Totals across successful runs |
| chunk_runs_by_status | Persisted chunk runs, labeled PENDING/RUNNING/SUCCEEDED/FAILED/NEEDS_REVIEW/CANCELLED |
| chunk_runs_by_result | Persisted chunk validation results, labeled PASS/PASS_WITH_WARNINGS/NEEDS_REVIEW/FAIL |
| chunk_validation_findings | Persisted chunk findings, labeled by severity |
| chunks_by_type | Persisted chunks, labeled by chunk type |
| question_artifacts_total | Persisted question objects |
| embedding_runs_by_status | Persisted embedding runs, labeled PENDING/RUNNING/SUCCEEDED/FAILED/NEEDS_REVIEW/CANCELLED |
| index_runs_by_status | Persisted index runs, labeled STAGING/VERIFYING/VERIFIED/FAILED/CANCELLED/SUPERSEDED |
| index_validation_findings | Persisted embedding and index findings, labeled by severity |
| embeddings_total, embeddings_reused_total | Persisted vectors, and how many were reused rather than recomputed |
| embedding_input_tokens_total | Total input tokens measured across persisted vectors |
| index_active_points_total | Verified points across every currently active index run |

Parsing, chunking and embedding run in the Celery worker, which is not scraped. The parse, chunk
and index series above are therefore derived from committed PostgreSQL rows at scrape time, which makes them accurate across worker
restarts rather than per-process. The worker also maintains in-process counters
(`parse_jobs_total`, `parse_jobs_succeeded_total`, `parse_jobs_failed_total{code}`,
`parse_jobs_needs_review_total`, `parse_jobs_cancelled_total`, `parse_duration_seconds`); those
are not exposed on an endpoint yet, so parse duration is not currently scrapeable. Document,
version, tenant, chunk, parse-run, chunk-run, embedding-run and index-run identifiers are never
metric labels; only fixed enum values and error codes are. None of the chunk or index series is a
retrieval or medical accuracy measure. Per-batch embedding throughput and duration are measured
during a run and recorded in `EmbeddingRun.metrics` and `IndexRun.metrics`; they are not exposed as
scrapeable histograms yet, for the same reason as parse duration.

In-process counters reset on process restart; they are not lifetime accounting. Status/failure
collectors query PostgreSQL. Worker receipt errors remain visible in durable job events; the upload
storage counter is not a complete cross-process storage metric. Scrape failures do not prove that
no jobs exist. HTTP counters and dependency checks remain available.

Operations uses real paginated job data, valid retry/reparse/rechunk/cancel controls,
correlation/error/history and receipt time, and offers the real executable stages as filters.
Document details show the actual parser build, policy version, page/element/table/figure/formula
counts, OCR usage and validation outcome for the current parse run, and the actual chunker build,
chunk policy, parent/child/table/formula/figure/question counts and validation outcome for the
current chunk run, and the actual model, revision, vector semantics, eligible/embedded/reused/
failed counts and verified point counts for the current embedding and index runs. The Chunk
Inspector shows measured tokens, content hashes and the exact source spans behind every chunk; the
Index Inspector shows the collection, vector name, dimension, metric, reconciliation counts and
live point counts read back from the index, and resolves any point to its chunk and source page.
No retrieval relevance score is displayed, because none exists. It also displays real dependency readiness. No
parsing percentage, accuracy figure or queue progress is invented; a version with no parse run
says so, a version with no chunk run says so, and a version with no embedding run says so. No
chunk or embedding quality percentage is invented, and neither a chunk nor an indexed point is
presented as retrievable evidence. Transfer percentage is based on browser byte events; subsequent validation/storage
uses stage text. API readiness checks dependencies, not worker progress or corpus activation.
Worker/dispatcher have no dedicated healthcheck; successful queue receipt is verified separately.

Use the JSON log configuration in the deployment commands. The formatter omits arbitrary message
bodies, exception contents, credentials and uploaded text. IDs are not metric labels.
There is no OpenTelemetry exporter, installed Prometheus server, alert routing or processing-stage
trace pipeline. `infrastructure/monitoring/prometheus.yml` is a scrape example.

## M6 telemetry

`evidence_stage_duration_seconds{stage}` uses bounded labels `reranking`, `expansion`,
`evidence_hydration`, and `assembly`. Failures use `retrieval_failures_total` with mode `RERANKED`
and declared codes. Traces retain M5 timings, model/library identity, query hash, policy snapshots/
fingerprints and total M6 duration. Source/query text is neither logged nor used as a metric label.

## M7 telemetry

`evidence_sufficiency_decisions_total{status}` counts gate decisions over the bounded label set
SUFFICIENT/INSUFFICIENT/CONFLICTING. Declared generation failures use `retrieval_failures_total`
with mode `GROUNDED_DRAFT`. Structured logs carry the correlation id, the status, the question kind,
the declared reason codes and the policy fingerprint. The question, the evidence text, the generated
draft, provider error bodies and every API key stay out of both logs and metric labels; a provider
error body is not echoed because it can quote the prompt back.
