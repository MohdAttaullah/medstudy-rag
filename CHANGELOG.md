# Changelog

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
