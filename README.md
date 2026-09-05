# Medical Evidence Workspace

Accuracy-first educational medical RAG. **Unsupported answers are unacceptable; abstention is a
successful outcome.** M1 implements authorized PDF upload, publication/version metadata and durable
ingestion tracking. Successful jobs stop at **QUEUED**. Medical answering remains disabled.

The stack is FastAPI/Pydantic/SQLAlchemy, React/TypeScript/Vite/TanStack Query, PostgreSQL,
Redis/Celery and MinIO/S3. Qdrant is available locally but unused by M1. Docling, OCR, chunks,
embeddings, retrieval and generation are future milestones.

## Start locally

Requires Python 3.12/3.13, uv, Node >=22.12 and Docker Compose v2 with a running Linux engine.
From the repository root:

```powershell
python scripts/init_local_env.py
python scripts/init_dev_auth.py
uv sync --frozen
docker compose up -d --wait postgres qdrant redis minio
docker compose run --rm minio-init
uv run --env-file .env alembic upgrade head
docker compose --profile app up -d --build --wait
```

Open http://localhost:5173/library. Read your generated **local development access keys** from
ignored `.local/dev-access.txt`; enter the admin key in the Library access form. The reader key
demonstrates read-only access. Scripts preserve existing credentials. Never commit or print keys.
The browser retains its key in tab memory only; reauthenticate after reload.

Select a synthetic PDF, supply a title, source type and authority, then upload. Library and document
details show the actual version, checksum and ingestion history. Operations shows jobs, errors,
retry/cancel controls and worker receipt. Receipt leaves the job QUEUED; it does not indicate parsing.
Assessment material cannot claim reference/high authority. Upload metadata defaults to UNREVIEWED.

For host development, run the API and Vite instead of their containers. Keep infrastructure and
the worker/dispatcher running; see [deployment](docs/architecture/deployment.md).

## Upload API

Authenticated endpoints use `/api/v1`. Upload with `POST /documents` or
`POST /documents/{id}/versions`: the body is raw PDF bytes, Content-Type is `application/pdf`,
`Idempotency-Key` is a UUID, and `X-Upload-Metadata` is base64-encoded UTF-8 JSON (12,000-character
encoded limit). New-publication metadata has this shape:

```json
{
  "filename": "source.pdf",
  "edition": "First",
  "publication_year": 2025,
  "document": {
    "title": "Synthetic reference",
    "source_type": "TEXTBOOK",
    "authority_level": "UNREVIEWED"
  }
}
```

For a new version, omit `document`; publication metadata is edited separately. Uploads are limited
to 128 MiB by default. Identical bytes within the same tenant return a deterministic duplicate
response. A network retry with identical metadata/content and request key returns the same IDs.
A confirmed failed attempt can use a new key; the UI handles this response. Sources are downloaded
through authorized API requests, never public bucket URLs.

See [M1 report](docs/verification/m1.md) for all endpoints and
[ingestion architecture](docs/architecture/ingestion.md) for recovery semantics.

## Verify

With development infrastructure running:

```powershell
$env:MEDRAG_RUN_INTEGRATION = '1'
uv run --env-file .env pytest -q
uv run ruff check backend workers scripts
uv run ruff format --check backend workers scripts
uv run mypy backend/app
uv run python scripts/check_skills.py
uv run --env-file .env alembic check
docker compose config --quiet
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
$env:MEDRAG_E2E_LIVE = '1'
$env:PLAYWRIGHT_CHANNEL = 'chrome'
uv run --env-file .env npm.cmd --prefix frontend run test:e2e
```

Browser tests use installed Chrome and Vite port 4173, while the container UI remains on 5173.
Alternatively install Playwright Chromium and omit the channel override. Live browser tests require
the migrated API, worker and dispatcher. They create synthetic documents in the development tenant.
Backend integration tests use a temporary PostgreSQL schema and their own UUID object keys; schema
upgrade/downgrade and failure cleanup do not erase application data. Without the integration flag,
live tests skip; a unit-only result does not verify persistence or queues.

## Repository map

| Path | Purpose |
|---|---|
| `backend/app/api`, `schemas`, `security` | Versioned contracts and server-side authorization |
| `backend/app/models`, `repositories`, `services` | Metadata, storage saga, job controls and outbox |
| `backend/app/ingestion` | Guarded states and bounded basic PDF validation |
| `frontend/src/features/library`, `operations` | Upload, publication/version details and real jobs |
| `workers` | Celery receipt task and durable outbox/recovery dispatcher |
| `migrations/versions` | Explicit PostgreSQL schema and ordered history migrations |
| `backend/tests`, `frontend/e2e` | Synthetic fixtures, regressions and live browser flows |
| `docs/architecture`, `docs/adr`, `docs/verification` | Design decisions and verified milestone report |
| `.agents/skills`, `.claude/skills` | Nine synchronized project skill pairs |

Read [AGENTS.md](AGENTS.md), [AI_HANDOFF.md](AI_HANDOFF.md) and relevant ADRs before changes.
The [M1 requirements](docs/requirements/m1.md) and original bootstrap request are preserved.

This is a local development implementation: development bearer identities are not production OIDC,
basic PDF checks are not antivirus, and there is no clinical validation or compliance certification.
