# Observability

M1 added persisted ingestion history and security/operations audit to M0 HTTP health telemetry;
M2 adds parse-stage tracing, parse audit and durable parse metrics.
UUID request IDs are returned as X-Request-ID; only syntactically valid caller IDs are accepted.
The upload ID follows validation/checksum/storage, committed job, outbox and receipt.

Safe structured phase events include UPLOAD_CHECKSUM_COMPLETED, UPLOAD_VALIDATION_COMPLETED,
UPLOAD_STORAGE_VERIFIED, UPLOAD_COMMITTED, INGESTION_QUEUE_PUBLISHED, INGESTION_QUEUE_UNAVAILABLE
and, for M2, PARSE_RUN_STARTED, PARSE_RAW_ARTIFACT_STORED, PARSE_NORMALIZED, PARSE_RUN_COMPLETED
and PARSE_RUN_FAILED. Together they trace the parse path: Celery receipt, source download, parser
initialization, conversion, raw artifact write, normalization, database persistence, artifact
extraction, validation and the state transition. Audit emits document/version/job creation, upload
rejection, duplicate detection, every state transition, retry, reparse request, cancel, archive,
metadata change, reconciliation, queue receipt, parse start, parse completion (with result and
counters), parse failure (code only) and parse reuse. Events carry
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

Parsing runs in the Celery worker, which is not scraped. The parse series above are therefore
derived from committed PostgreSQL rows at scrape time, which makes them accurate across worker
restarts rather than per-process. The worker also maintains in-process counters
(`parse_jobs_total`, `parse_jobs_succeeded_total`, `parse_jobs_failed_total{code}`,
`parse_jobs_needs_review_total`, `parse_jobs_cancelled_total`, `parse_duration_seconds`); those
are not exposed on an endpoint yet, so parse duration is not currently scrapeable. Document,
version, tenant and parse-run identifiers are never metric labels; only fixed enum values and
error codes are.

In-process counters reset on process restart; they are not lifetime accounting. Status/failure
collectors query PostgreSQL. Worker receipt errors remain visible in durable job events; the upload
storage counter is not a complete cross-process storage metric. Scrape failures do not prove that
no jobs exist. HTTP counters and dependency checks remain available.

Operations uses real paginated job data, valid retry/reparse/cancel controls, correlation/error/
history and receipt time, and offers the real executable stages as filters. Document details show
the actual parser build, policy version, page/element/table/figure/formula counts, OCR usage and
validation outcome for the current parse run. It also displays real dependency readiness. No
parsing percentage, accuracy figure or queue progress is invented; a version with no parse run
says so. Transfer percentage is based on browser byte events; subsequent validation/storage
uses stage text. API readiness checks dependencies, not worker progress or corpus activation.
Worker/dispatcher have no dedicated healthcheck; successful queue receipt is verified separately.

Use the JSON log configuration in the deployment commands. The formatter omits arbitrary message
bodies, exception contents, credentials and uploaded text. IDs are not metric labels.
There is no OpenTelemetry exporter, installed Prometheus server, alert routing or processing-stage
trace pipeline. `infrastructure/monitoring/prometheus.yml` is a scrape example.
