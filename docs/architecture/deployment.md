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
MinIO initialization creates a private bucket and enables versioning. Apply all six revisions;
current head is `m5_hybrid_retrieval`. A pre-M1 schema has no domain tables to preserve;
existing data is upgraded in place. Do not downgrade application data merely to rerun a test: each
milestone downgrade deliberately refuses to run while any job still holds one of the states that
milestone introduced — READY_FOR_CHUNKING for M2, the chunk states for M3,
EMBEDDING/INDEXING/VERIFYING_INDEX/READY_FOR_RETRIEVAL for M4, and
SPARSE_INDEXING/VERIFYING_SPARSE_INDEX/RETRIEVAL_READY for M5. Cancel those jobs first, which is
the operator action the refusal message prescribes; nothing is ever silently rewritten.

M5 is the one revision that widens the status columns, because `VERIFYING_SPARSE_INDEX` is longer
than any status before it. Widening requires dropping the M3 `m3_job_cancelled` trigger, which is
declared `AFTER UPDATE OF status`, and recreating it immediately afterwards; the function it calls
is untouched. Expect that drop/recreate in the migration output and do not interrupt it.

The app profile includes API, frontend, Celery worker, the outbox/recovery dispatcher and the
retrieval query-encoder service. The worker and dispatcher also belong to the workers profile.
API/frontend/dependency healthchecks do not prove queue processing. Confirm receipt in Operations
or run the live browser test. QUEUED with no receipt while the broker is unavailable is durable
pending delivery, not data loss.

## Ingestion worker image

Only the Celery worker parses, so only it carries the parser stack.
`infrastructure/docker/worker.Dockerfile` installs the `parsing` extra plus the X/GL runtime
libraries the OCR engine's OpenCV wheel links against, which are absent from `python:3.12-slim`
and whose absence fails OCR at model initialisation. The API and dispatcher stay on
`backend.Dockerfile` and are unaffected in size or startup time.

On Linux, torch and torchvision resolve from the PyTorch CPU index (`[tool.uv.sources]` in
`pyproject.toml`) because this deployment has no GPU; the default resolution would add several
gigabytes of CUDA runtime for no benefit. The worker carries the `parsing` and `embedding`
extras, so it holds the Docling stack and the MedCPT encoder; the resulting image is about 2.8 GB.

Two model caches, provisioned differently, both created in the image so the named volumes inherit
their ownership on first mount:

* `parser-models` at `/home/medrag/.cache` — Docling weights, **downloaded on first use**. The
  first parse of a fresh volume therefore needs outbound network access and is noticeably slower.
* `embedding-models` at `/home/medrag/models/embeddings` — the pinned MedCPT revision,
  **provisioned deliberately** and then read offline.

Provision the embedding cache once, before the first document needs it:

```powershell
docker run --rm --user 10001 `
  -v medical-rag_embedding-models:/home/medrag/models/embeddings `
  -v ${PWD}/scripts:/repo/scripts:ro -v ${PWD}/backend:/repo/backend:ro -w /repo `
  medical-rag-worker:latest python /repo/scripts/provision_embedding_model.py `
  --cache /home/medrag/models/embeddings
```

It downloads exactly the pinned revision and verifies the weight checksum. The worker then runs
with `MEDRAG_EMBEDDING__OFFLINE=true`, and a model absent from that cache fails closed rather than
reaching the network, so no user request depends on a runtime download. Weights are not baked into
the image. The parser has no equivalent offline mode yet.

Rebuild the worker after changing dependencies:

```powershell
docker compose --profile app up -d --build --wait worker
```

## Vector index

Qdrant runs as a single local node with its storage in the `qdrant-data` volume. It holds real
collections from M4 onwards, named `medrag_chunks_<schema version>_<semantics fingerprint>`, with a
`medrag_chunks_active` alias. It is **internal infrastructure**: ports 6333/6334 are published for
local development only and must not be exposed publicly, the browser never talks to it, and there
is no API key, TLS, replication or backup in this deployment. Deleting the `qdrant-data` volume
discards every index; the vectors can be rebuilt with an explicit re-embed, since PostgreSQL holds
the authoritative record of what should exist.

Changing the embedding model or any vector semantics is a **reindex-required** change: it creates a
new EmbeddingVersion and a new physical collection alongside the current one, and the alias switches
only after the replacement verifies. A failed replacement leaves the previous active index in place.

## Retrieval query service

Interactive query encoding runs in its own container, `retrieval`, built from the worker image and
started with `uvicorn app.retrieval_service:create_app --factory` on port 8010. The reason is
concrete rather than stylistic: the API and dispatcher are built from the lean backend image and
carry no torch at all, and the ingestion worker already holds warm *article* encoder weights it
would never use for a question. Loading a second transformer into every web worker would add
hundreds of megabytes per process to serve a request that spends most of its time in PostgreSQL.

Its responsibilities are query-vector encoding and query/passage ranking. It has no database
connection, no tenant concept, no authorization and no document access, so it cannot become a
second place where access decisions are made. Authorization, tenant scoping, corpus resolution,
filtering and hydration all stay in the API next to the authenticated principal. Like Qdrant it is
**internal infrastructure**: its port is not published, the browser never reaches it, and it never
logs query text.

Its weights come from the `query-models` volume, provisioned deliberately:

```powershell
docker compose run --rm --no-deps --entrypoint python retrieval `
  /repo/scripts/provision_query_model.py --cache /home/medrag/models/embeddings
```

The service then runs with `MEDRAG_QUERY_ENCODER__OFFLINE=true`, and a model absent from that cache
fails closed rather than reaching the network. The API reaches it through
`MEDRAG_QUERY_ENCODER__ENDPOINT`; leaving that empty makes the API load the encoder in-process
instead, which is what the evaluation harness and the tests use and what a torch-less API image
cannot do. A `BM25_ONLY` query never calls this service at all.

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

## M6 private reranker runtime

The same private retrieval service now keeps Query Encoder and MedCPT CrossEncoder warm; Article
Encoder remains in the ingestion worker. A separate `reranker-models` volume mounts at
`/home/medrag/models/reranking`, owned by UID/GID 10001. All seven pinned files are checksummed
before restricted `weights_only=True` loading. Interactive resolution is always local-only.

Provision with `uv run --extra embedding python scripts/provision_reranker_model.py`, then copy the
cache into that named volume. Alternatively run the script in the retrieval image with the
repository mounted read-only and the cache volume writable. Retain caches across replacements.
Compose readiness checks both `/health/ready` and `/health/reranker`; `/health/live` only describes
process liveness. No model port is published. API owns authorization. Runtime settings require
restart. See M6 verification for measured memory and host/Linux differences. Serialize heavyweight
model/image checks on the 16 GB development host.

## M7 provider access

Generation lives only in the API, so `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and the generator
selection reach only the `api` service. They are deliberately not in the shared environment anchor:
the dispatcher, worker, private retrieval runtime and frontend have no use for them, and a key that
is never delivered cannot leak from those containers. All four default to empty, which leaves the
generator unconfigured — a declared unavailable state, not a fallback to some default model — so
the stack starts and the sufficiency gate still runs without any provider account. No provider call
is made unless the gate returns SUFFICIENT. Keys are never baked into an image; a `VITE_`-prefixed
copy would be compiled into the browser bundle and is forbidden.


## M10 configuration

M10 requires migration `m10_configuration` before the API starts. `MEDRAG_APPROVED_MODELS` and
explicit verifier selection are delivered only to the API. Rebuild the API and frontend images.
Runtime tenant overrides are read per request; shared restart settings stay externally managed. No
restart or rebuild control is exposed in the UI.
