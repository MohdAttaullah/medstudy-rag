# AI HANDOFF

## Current milestone and status

M1 - Authorized upload and durable ingestion control plane: COMPLETE and verified locally.
M0 remains verified. Successful jobs stop at UPLOADED -> VALIDATING -> QUEUED.
Medical answering is disabled. M2 has not started.

## Completed implementation

- Preserved existing backend/domain work during recovery; completed missing frontend/proxy/container
  wiring from actual repository state. No reset or project recreation.
- Versioned authorized APIs, development AuthProvider/Principal adapter, tenant-scoped reader/curator/
  admin permissions, strict source/authority metadata and private source download.
- Normalized documents/immutable versions/jobs, streamed PDF validation and SHA-256, tenant-local
  duplicates, durable upload intents, idempotency and storage/database compensation/reconciliation.
- Guarded job transitions, config snapshots, append-only audit and explicitly sequenced stage history.
- Transactional outbox dispatcher and real Celery receipt; bounded retry, cancel/archive, stale and
  duplicate delivery fencing. Receipt never starts parsing or makes a version searchable.
- Functional Library upload/list/archive, publication details/edit/version/source/history and
  Operations jobs/actions/errors. Actual transfer progress; safe status on replay. Network retries
  preserve keys; confirmed failed attempts use new keys.
- Safe request/phase/queue logs, persisted status/failure metrics, synthetic fixtures, live tests,
  migrations and current architecture/ADR/README documentation.

Modules: backend/app/{api,schemas,security,models,repositories,services,ingestion,observability};
workers/{celery_app,bootstrap,dispatcher}; frontend/src/features/{library,operations};
migrations/versions; scripts/{init_dev_auth,create_test_pdfs,smoke_m1}.
See [M1 report](docs/verification/m1.md) for complete endpoint/module and verification details.

## Git and files

Branch: main. No baseline commit or last-known-good commit exists.
All project files are new/untracked; nothing staged or committed. AGENTS.md and CLAUDE.md preserved.
Changed areas include root config/docs, backend, frontend, workers, migrations, infrastructure,
scripts and the original M0 skill directories. .env, .local, caches, dependencies, build outputs and
browser artifacts are ignored. No secrets were staged.

Host-account Git commands may need the scoped override because sandbox identity initialized Git:
`git -c safe.directory=D:/Projects/RAG/Proj_1_DoctorDocuments status --short --branch`.
No global Git configuration was changed.

## Verification executed

Commands ran from repository root; local invocations used --cache-dir .uv-cache.

| Check | Final result |
|---|---|
| MEDRAG_RUN_INTEGRATION=1; uv run --env-file .env pytest backend/tests/test_m1_units.py backend/tests/test_m1_integration.py -q | 42 passed |
| MEDRAG_RUN_INTEGRATION=1; uv run --env-file .env pytest -q | 59 passed, no skipped tests |
| uv run ruff check backend workers scripts | Passed |
| uv run ruff format --check backend workers scripts | Passed, 81 files |
| uv run mypy backend/app | Passed, 61 source files |
| uv run python scripts/check_skills.py | 9 synchronized pairs passed |
| uv run --env-file .env alembic check | No drift |
| uv run --env-file .env alembic current | m1_event_order (head) |
| npm --prefix frontend test | 14 passed |
| npm --prefix frontend run build | TypeScript/Vite passed |
| MEDRAG_E2E_LIVE=1; PLAYWRIGHT_CHANNEL=chrome; uv run --env-file .env npm.cmd --prefix frontend run test:e2e | 2 passed, 11.9 seconds |
| docker compose config --quiet | Passed |
| docker compose --profile app up -d --build --wait | Final app/worker/dispatcher built and started |
| docker compose --profile app up -d --build --no-deps --wait frontend | Final frontend refreshed, healthy |
| uv run --env-file .env python scripts/smoke_m1.py | Direct API and nginx auth/list/source/upload-validation/deep-link checks passed |
| Safe structured logs | Upload checksum/validation/storage/commit and queue publish/receipt present |

Backend warnings: two upstream Starlette/httpx/AnyIO deprecations. Earlier browser locator and
obsolete success-message assertions were corrected; final run passes. Browser uses installed Chrome
because the earlier M0 Chromium download timed out. It runs Vite on 4173 beside container UI 5173.
Desktop/mobile browser artifacts are under ignored frontend/test-results; layouts inspected.

## Migrations and infrastructure

Applied to the development database:
- 0dd8e0dcb035: nine domain/control tables, constraints/indexes, append-only and immutable/state guards.
- m1_event_order: sequence backfill and unique per-job history order; current head.

Integration tests exercise upgrade/downgrade/upgrade in disposable random schemas and clean their own
object keys. No application data reset. Startup never uses create_all; migrate before app/worker use.

Running: API 127.0.0.1:8000; frontend 127.0.0.1:5173; PostgreSQL 5432; Redis 6379;
MinIO 9000/9001; Qdrant 6333/6334; worker and outbox dispatcher. API/frontend/PostgreSQL/Redis/MinIO
healthchecks pass, all four dependency protocols pass. Worker/dispatcher receipt is verified by the
live browser test; no dedicated worker healthcheck exists. MinIO initializer exited zero.
Versioned private source bucket and named volumes persist.

Generated credentials remain in ignored .env. UI admin/reader keys are in .local/dev-access.txt;
do not print them. scripts/init_dev_auth.py preserves existing keys. Browser tests leave synthetic
publications in the local tenant. scripts/smoke_m1.py expects at least one such existing document.
No user medical corpus was imported.

## Decisions, limitations and blockers

ADR-001 through ADR-005 remain accepted. ADR-006 records the upload-intent saga, receipt-only outbox,
raw-body/base64-header upload protocol, development authorization and ordered history.
M1 versions are database-constrained unsearchable. Future processing states cannot execute.

No remaining M1 blocker. This is local development: no production OIDC, credential lifecycle,
AV/comprehensive PDF sanitization, dedicated parser memory sandbox, upload quotas, TLS, managed
secrets, backup/retention/purge, full audit UI or installed OpenTelemetry/alert pipeline.
Validation has byte/time limits; source checks compare stored pinned metadata. These controls are
not clinical validation or compliance certification. Production startup remains explicitly rejected.
Qdrant is available but no index/corpus exists; provider selection remains absent.

The resumed session's normal sandbox helper failed to initialize. Authorized host shell operations
were used after review; no previous declined bulk command was blindly replayed. No current approval
or account-limit block remains.

## Next exact task

Stop. Wait for the user's M2 authorization. Then begin:
**M2 - Docling parsing, structured document normalization, page/element provenance and parsing-quality
validation.**

Read AGENTS.md, this handoff, ingestion/data-model/security docs and ADR-001/006. Use existing version,
job, config and outbox boundaries. Define M2 structured artifact schema, parser/version configuration,
leases/fencing and quality validation before permitting new executable transitions. Preserve originals,
ordered history, tenant isolation and failure recovery. Add reviewed migrations and focused fixtures.
Do not skip ahead into chunks, embeddings, indexing, retrieval or answering.

## Do not do

- Do not expose partially processed versions or enable medical answering.
- Do not fabricate parsing/chunk/index progress or clinical accuracy.
- Do not promote question keys/generated metadata to authoritative medical evidence.
- Do not delete named volumes, rotate credentials by editing .env, reset/discard uncommitted work,
  overwrite durable instruction files, or claim checks that were not run.
- Do not commit secrets or expose the development stack publicly.

Last updated: 2026-09-05 by Codex.
