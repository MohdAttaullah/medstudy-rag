# Medical Evidence Workspace

Accuracy-first educational medical RAG. **Unsupported answers are unacceptable; abstention is a
successful outcome.** M1 implements authorized PDF upload, publication/version metadata and durable
ingestion tracking; M2 adds Docling parsing, structured normalization with page and coordinate
provenance, and parse-quality validation; M3 adds structure-aware hierarchical chunking with
complete span-level provenance, deterministic chunk identity and chunk-quality validation; M4 adds
pinned MedCPT dense embeddings, a versioned Qdrant index and verified index activation.
Successful jobs stop at **READY_FOR_RETRIEVAL**, which means the vectors were built and the index
reconciled — **not** that the document is answerable. Medical answering remains disabled.

The stack is FastAPI/Pydantic/SQLAlchemy, React/TypeScript/Vite/TanStack Query, PostgreSQL,
Redis/Celery, MinIO/S3, Docling and Qdrant. Query retrieval, reranking and generation are future
milestones.

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
details show the actual version, checksum, ingestion history and, once the worker has parsed it, the
real parser build, policy version, page/element/table/figure/formula counts, OCR usage and
validation outcome. "Open parse inspector" shows the parsed pages, structured elements with their
page coordinates, extracted tables, figure crops, formulas and validation findings. Operations shows
jobs, errors and retry/reparse/cancel controls. Assessment material cannot claim reference/high
authority. Upload metadata defaults to UNREVIEWED.

The parsing worker downloads model weights on its first document into the `parser-models` volume,
so the first parse after a fresh volume takes noticeably longer and needs outbound network access.

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

Parsed material is inspected through `GET /documents/{id}/versions/{vid}/parse` and
`/parse-runs/...` (runs, pages, page previews, elements, tables, figures, formulas, findings and the
raw parser artifact). Every route enforces the same tenant boundary as the original, and object
storage is never exposed.

Chunked material is inspected through `GET /documents/{id}/versions/{vid}/chunk-runs` and
`/chunk-runs/...` and `/chunks/...` (runs, chunks with type/page/relationship filters, chunk detail,
ordered source mappings, question objects and validation findings). No route returns a storage key,
a lease token or any vector field.

Embedding and index state is inspected through `GET /documents/{id}/versions/{vid}/embedding`,
`/embedding-runs/...` and `/index-runs/...` (runs, vector metadata, index runs, live index
statistics and validation findings). No route returns a dense vector, and the browser never talks
to Qdrant.

See the [M1](docs/verification/m1.md), [M2](docs/verification/m2.md),
[M3](docs/verification/m3.md) and [M4](docs/verification/m4.md) reports for all endpoints,
[ingestion architecture](docs/architecture/ingestion.md) for recovery semantics,
[document parsing](docs/architecture/document-parsing.md) for the parse contract,
[document chunking](docs/architecture/document-chunking.md) for the chunk contract, and
[embeddings](docs/architecture/embeddings.md) and
[vector index](docs/architecture/vector-index.md) for the vector contract.

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
uv run --env-file .env python scripts/smoke_m1.py
uv run --env-file .env python scripts/smoke_m2.py
uv run --env-file .env python scripts/smoke_m3.py
uv run --env-file .env python scripts/smoke_m4.py
uv run --extra parsing python scripts/evaluate_parsing.py
uv run python scripts/evaluate_chunking.py
uv run --extra embedding python scripts/evaluate_embeddings.py
```

Embedding needs its model provisioned once, which is the only step that reaches the network:

```powershell
uv run --extra embedding python scripts/provision_embedding_model.py
```

The containerised worker reads the same pinned revision from the `embedding-models` volume and runs
with downloads disabled. See [embeddings](docs/architecture/embeddings.md) for the provisioning
workflow.

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
| `backend/app/ingestion` | Guarded states, PDF validation, parser abstraction, normalization and parse-quality rules |
| `backend/app/evaluation` | Parsing extraction-fidelity evaluator |
| `frontend/src/features/parsing` | Parse summary and parse inspector |
| `frontend/src/features/library`, `operations` | Upload, publication/version details and real jobs |
| `workers` | Celery receipt task and durable outbox/recovery dispatcher |
| `migrations/versions` | Explicit PostgreSQL schema and ordered history migrations |
| `backend/tests`, `frontend/e2e` | Synthetic fixtures, regressions and live browser flows |
| `docs/architecture`, `docs/adr`, `docs/verification` | Design decisions and verified milestone report |
| `.agents/skills`, `.claude/skills` | Nine synchronized project skill pairs |

Read [AGENTS.md](AGENTS.md), [AI_HANDOFF.md](AI_HANDOFF.md) and relevant ADRs before changes.
The [M1](docs/requirements/m1.md), [M2](docs/requirements/m2.md),
[M3](docs/requirements/m3.md) and [M4](docs/requirements/m4.md) requirements and the original
bootstrap request are preserved.

This is a local development implementation: development bearer identities are not production OIDC,
basic PDF checks are not antivirus, parse-, chunk- and index-quality thresholds are uncalibrated
defaults chosen against synthetic fixtures, retrieval quality has not been measured at all, and
there is no clinical validation or compliance certification.
