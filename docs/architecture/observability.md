# Observability

M1 adds persisted ingestion history and security/operations audit to M0 HTTP health telemetry.
UUID request IDs are returned as X-Request-ID; only syntactically valid caller IDs are accepted.
The upload ID follows validation/checksum/storage, committed job, outbox and receipt.

Safe structured phase events include UPLOAD_CHECKSUM_COMPLETED, UPLOAD_VALIDATION_COMPLETED,
UPLOAD_STORAGE_VERIFIED, UPLOAD_COMMITTED, INGESTION_QUEUE_PUBLISHED and INGESTION_QUEUE_UNAVAILABLE.
Audit emits document/version/job creation, upload rejection, duplicate detection, every state
transition, retry, cancel, archive, metadata change, reconciliation and queue receipt. Events carry
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

In-process counters reset on process restart; they are not lifetime accounting. Status/failure
collectors query PostgreSQL. Worker receipt errors remain visible in durable job events; the upload
storage counter is not a complete cross-process storage metric. Scrape failures do not prove that
no jobs exist. HTTP counters and dependency checks remain available.

Operations uses real paginated job data, valid retry/cancel controls, correlation/error/history and
receipt time. It also displays real dependency readiness. No parsing percentage or queue progress
is invented. Transfer percentage is based on browser byte events; subsequent validation/storage
uses stage text. API readiness checks dependencies, not worker progress or corpus activation.
Worker/dispatcher have no dedicated healthcheck; successful queue receipt is verified separately.

Use the JSON log configuration in the deployment commands. The formatter omits arbitrary message
bodies, exception contents, credentials and uploaded text. IDs are not metric labels.
There is no OpenTelemetry exporter, installed Prometheus server, alert routing or processing-stage
trace pipeline. `infrastructure/monitoring/prometheus.yml` is a scrape example.
