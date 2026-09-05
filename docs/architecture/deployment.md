# Development deployment

Requires Python 3.12/3.13, uv, Node >=22.12 and Docker Compose v2 with a running Linux engine.
Python/JS dependencies are locked. Image tags are development pins; production digest pinning,
scanning and promotion are future work.

## Container application

Run from the repository root:

```powershell
python scripts/init_local_env.py
python scripts/init_dev_auth.py
uv sync --frozen
docker compose config --quiet
docker compose up -d --wait postgres qdrant redis minio
docker compose run --rm minio-init
uv run --env-file .env alembic upgrade head
docker compose --profile app up -d --build --wait
```

Apply migrations before starting data endpoints/workers. No API startup schema mutation occurs.
MinIO initialization creates a private bucket and enables versioning. Apply both M1 revisions;
current head is m1_event_order. A pre-M1 schema has no domain tables to preserve; existing M1 data
is upgraded in place. Do not downgrade application data merely to rerun a test.

The app profile includes API, frontend, Celery worker and the outbox/recovery dispatcher. Both
background processes also belong to the workers profile. API/frontend/dependency healthchecks
do not prove queue processing. Confirm receipt in Operations or run the live browser test.
QUEUED with no receipt while the broker is unavailable is durable pending delivery, not data loss.

Open http://localhost:5173/library and read the generated admin/reader access keys from ignored
.local/dev-access.txt. Keys map to one development tenant and remain server configured in .env.
The setup scripts preserve existing values. Browser authentication lasts for the tab session.

## Host API/frontend

After initializing infrastructure and applying migrations, run the API in one terminal:

```powershell
uv run --env-file .env uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log --log-config infrastructure/monitoring/logging.json
```

In another:

```powershell
npm --prefix frontend ci
npm --prefix frontend run dev
```

Run worker/dispatcher containers alongside the host application with
`docker compose --profile workers up -d --build worker dispatcher`.
Stop conflicting API/frontend containers deliberately before binding the same ports; keep named data
volumes. Native Windows Celery is not the supported local path; the worker runs in Linux containers.

Vite and nginx preserve /api/v1 for document APIs. The existing /api/health prefix still proxies
to backend /health. Nginx disables request buffering for PDF upload, permits 16 KiB header buffers,
130 MiB request bodies and a 360-second proxy timeout. API defaults are 128 MiB and 300 seconds;
increase proxy limits too if deliberately increasing application limits. Metadata is limited to
12,000 base64 characters by the API.

## Operations and verification

Use explicit IPv4 loopback URLs on Windows: API 8000, UI 5173, PostgreSQL 5432, Redis 6379,
Qdrant 6333/6334 and MinIO 9000/9001. Compose maps internal service addresses separately.
Readiness checks all four dependency protocols; it does not assert corpus readiness.

```powershell
$env:MEDRAG_RUN_INTEGRATION = '1'
uv run --env-file .env pytest -q
$env:MEDRAG_E2E_LIVE = '1'
$env:PLAYWRIGHT_CHANNEL = 'chrome'
uv run --env-file .env npm.cmd --prefix frontend run test:e2e
```

Backend live tests migrate a disposable random PostgreSQL schema and clean only their own original
keys. Browser tests use a Vite server on 4173 and leave synthetic documents in the development tenant.
Use installed Chrome or install Playwright Chromium and omit the channel override.

`docker compose down` preserves named volumes. Volume deletion is a separate destructive operation.
Editing .env does not rotate an existing PostgreSQL password. Do not rotate credentials or delete
volumes to repair startup. The infrastructure-only startup separates long-running health waits from
the successful one-shot MinIO initializer.

No production OIDC, TLS, managed secrets, retention, backups, quotas or deployable Kubernetes stack
is included. Worker phase logs use the same safe formatter as API audit logs; dedicated worker
healthchecks and an installed monitoring/alert pipeline remain future work.
