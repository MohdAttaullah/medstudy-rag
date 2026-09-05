# Changelog

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
