# AI HANDOFF

## Current milestone and status

M2 - Docling parsing, parser-independent structured normalization, provenance and parse-quality
validation: COMPLETE and verified locally. M0 and M1 remain verified.
Successful jobs execute UPLOADED -> VALIDATING -> QUEUED -> PARSING -> NORMALIZING -> ENRICHING ->
READY_FOR_CHUNKING. READY_FOR_CHUNKING means parsed and validated, not retrievable: no chunk,
embedding or index exists and every version stays database-constrained unsearchable.
Medical answering is disabled. M3 has not started.

## Completed implementation

- Docling 2.126.0 behind a parser-independent abstraction; only `ingestion/parser/docling_adapter`
  imports Docling. Device and thread count pinned; remote services and external plugins disabled.
- Durable versioned ParseRun: parser name/provider/version, policy version plus a SHA-256
  fingerprint of the whole frozen policy, config snapshot, source checksum and pinned object
  version, raw artifact location, counters, lease and safe error. At most one active run per
  version, enforced by a partial unique index and a check constraint.
- Raw Docling artifact, page previews and figure crops in private object storage under
  `documents/<doc>/<version>/parsing/<run>/`; binary rasters are separate objects, not base64 in
  the JSON. PostgreSQL is never the only copy of the parse structure.
- Parser-independent pages, elements, tables, figures, formulas and findings with 1-based page
  numbers, TOPLEFT-origin point bounding boxes, parser-declared hierarchy, deterministic reading
  order, retained page headers/footers and preserved question-bank cues. Nothing is inferred.
- Deterministic normalization (NFKC, soft hyphens, ligatures, zero-width, hyphenated line breaks,
  whitespace) that never rewrites numbers, units, doses, terminology or formulas; the original
  parser text is kept whenever it differs.
- OCR configuration (OFF/AUTO/FORCE, RapidOCR) with recorded engine, per-page derived OCR
  indicator from the pypdf-measured source text layer, and suspicious-OCR review routing.
- Deterministic parse-quality layer: persisted findings and PASS / PASS_WITH_WARNINGS /
  NEEDS_REVIEW / FAIL, with typed thresholds inside the policy fingerprint. Fail-closed errors with
  declared retryability; deterministic input defects are never retried.
- Worker pipeline with idempotent delivery, cancellation handling, per-job temporary directories,
  parse leases, dispatcher lease reaping and an explicit permissioned reparse action.
- Tenant-authorized inspection APIs, parse summary on document details, parse inspector with page
  previews and table/figure/formula views, and real parse stages in Operations.
- Parsing extraction-fidelity gold dataset and evaluation harness, separate from RAG evaluation.

Modules: backend/app/{api,core,evaluation,ingestion,models,observability,repositories,schemas,
services}; workers/{celery_app,dispatcher}; frontend/src/features/parsing;
migrations/versions/m2_document_parsing; scripts/{create_parse_fixtures,evaluate_parsing,smoke_m2};
infrastructure/docker/worker.Dockerfile.
See [M2 report](docs/verification/m2.md) and [document parsing](docs/architecture/document-parsing.md).

## Git and files

Branch: main. Last known good commit: **bb2bfa0** ("feat: complete M1 document ingestion control
plane"), the verified M1 baseline and the only commit on the branch.

(The previous handoff stated that no baseline commit existed and that all files were untracked.
That was stale: bb2bfa0 exists and contains the whole M1 tree. Corrected here.)

All M2 work is **uncommitted** in the working tree; nothing is staged. No history was rewritten,
reset or force-pushed, and no prior work was discarded. `.env`, `.local`, caches, dependencies,
build outputs and browser artifacts remain ignored; no secrets are in the tree.

Codex's only change after bb2bfa0 was the `parsing` optional-dependency group plus `reportlab` in
`pyproject.toml`, with `.venv` synced but `uv.lock` not updated. This session completed that:
`uv.lock` now carries the full parsing resolution, and torch/torchvision are declared directly and
mapped to the PyTorch CPU index on Linux, which removed all CUDA packages from the lock.

Host-account Git commands may still need the scoped override because sandbox identity initialized
Git: `git -c safe.directory=D:/Projects/RAG/Proj_1_DoctorDocuments status --short --branch`.
No global Git configuration was changed.

## Verification executed

Commands ran from the repository root against the running development stack.

| Check | Final result |
|---|---|
| MEDRAG_RUN_INTEGRATION=1 pytest -q (M1 baseline, before any change) | 59 passed |
| MEDRAG_RUN_INTEGRATION=1 pytest -q (final) | 142 passed |
| ruff check / ruff format --check backend workers scripts | Passed, 99 files |
| mypy backend/app | Passed, 74 source files |
| python scripts/check_skills.py | 9 synchronized pairs passed |
| alembic current / alembic check | m2_document_parsing (head) / no drift |
| alembic downgrade m1_event_order -> upgrade head -> check | Passed |
| npm --prefix frontend test | 23 passed |
| npm --prefix frontend run build | TypeScript and Vite passed |
| MEDRAG_E2E_LIVE=1 PLAYWRIGHT_CHANNEL=chrome playwright test | 4 passed |
| docker compose config --quiet | Passed |
| docker compose --profile app up -d --build --wait | All services healthy |
| python scripts/smoke_m1.py | PASS |
| python scripts/smoke_m2.py | PASS (containerised worker parsed a real upload) |
| python scripts/evaluate_parsing.py (host) | 9/9 cases, 142/142 checks |
| python scripts/evaluate_parsing.py (Linux worker image) | 4/9 cases, 129/142 checks, see below |

Warnings: two upstream Starlette/httpx/AnyIO deprecations plus two Docling/pydantic deprecations
raised inside the vendor library. Two stale M1 assertions were updated for M2 reality (the liveness
milestone string, and QUEUED -> PARSING which is now legal); no test was disabled or weakened to
make the suite pass.

## Migrations and infrastructure

Applied to the development database: `0dd8e0dcb035`, `m1_event_order`, `m2_document_parsing`
(current head). The M2 revision adds seven parsing tables with their indexes, constraints and the
findings append-only trigger, widens the job/version status vocabulary and column width for
READY_FOR_CHUNKING, and replaces the M1 transition guard with the M2 graph including the explicit
reparse edge. Its downgrade refuses to run while any row still holds READY_FOR_CHUNKING rather than
silently rewriting it; the M2 integration teardown cancels such jobs first, which is the documented
operator action.

Running: API 127.0.0.1:8000; frontend 127.0.0.1:5173; PostgreSQL 5432; Redis 6379; MinIO 9000/9001;
Qdrant 6333/6334; parsing worker and outbox dispatcher. Only the worker carries the parser: it
builds from `infrastructure/docker/worker.Dockerfile` (parsing extra plus the X/GL libraries
OpenCV links against) and is ~2.5 GB. Model weights live in the `parser-models` named volume at
`/home/medrag/.cache`; the first parse of a fresh volume needs outbound network access and is
noticeably slower. Two container-only defects were found and fixed during verification: missing
`libxcb.so.1`/`libgl1` failed OCR at model init, and the cache volume mounted root-owned.

Generated credentials remain in ignored `.env`; UI keys are in `.local/dev-access.txt` — do not
print them. Browser and smoke tests leave synthetic publications in the local tenant. No user
medical corpus was imported.

## Decisions, limitations and blockers

ADR-001 through ADR-006 remain accepted. **ADR-007** records versioned parse runs, derived-artifact
storage and fail-closed parse validation.

**Important finding for the next agent.** Layout classification is not bit-identical across
CPU/BLAS environments. On the same parser version and the same bytes, the Windows host and the
Linux worker container agreed on page counts, page text, table cell contents, grid geometry and
reading order, but disagreed on semantic labels for borderline regions: page header/footer
classification, table caption association, formula detection and list grouping. Consequently the
gold dataset is baselined per environment (host 9/9, container 4/9) and the automated tests assert
our own persistence, provenance and authorization, using a fixed parser output where a specific
parser judgement is required. Do not "fix" this by loosening the gold dataset into vagueness, and
do not claim environment-independent structural determinism.

Other limitations: thresholds are uncalibrated defaults chosen against synthetic fixtures; there is
no review-approval workflow (a NEEDS_REVIEW document is reparsed or cancelled, and reparse consumes
the bounded retry budget); no retention or cleanup of superseded runs and their artifacts; the
parser has a timeout, page guard and artifact ceiling but no hard memory sandbox or per-tenant
quota; worker in-process parse metrics are not scrapeable (the exposed parse series are derived
from PostgreSQL); there is no offline model mode. This remains local development: no production
OIDC, credential lifecycle, AV scanning, TLS, managed secrets, backup/retention/purge or installed
telemetry pipeline. None of this is clinical validation or compliance certification. Production
startup remains explicitly rejected. Qdrant is available but no index or corpus exists.

No remaining M2 blocker.

## Next exact task

Stop. Wait for the user's M3 authorization. Then begin:
**M3 - Structure-aware hierarchical medical chunking, specialized tables/formulas/figures,
question-bank logical objects, parent-child relationships, and chunk-quality evaluation.**

Read AGENTS.md, this handoff, [document parsing](docs/architecture/document-parsing.md),
[data model](docs/architecture/data-model.md), ADR-001/006/007 and the retrieval-quality skill.
Consume only the **active** ParseRun (`is_active`) and its persisted elements, tables, figures and
formulas; the raw Docling artifact exists for debugging and re-normalization, not as a chunking
input. Preserve page numbers, bounding boxes, reading order, hierarchy and caption relations onto
chunks so citations resolve to an exact source region. Add the `CHUNKING` edge to both the
application transition table and the database guard in a reviewed migration; it is deliberately
absent today. Add chunk-quality evaluation separate from parsing evaluation and from RAG
evaluation. Do not skip ahead into embeddings, indexing, retrieval or answering.

## Do not do

- Do not expose partially processed versions or enable medical answering.
- Do not fabricate parse, chunk or index progress, accuracy percentages or clinical accuracy.
- Do not promote question keys, captions or generated metadata to authoritative medical evidence.
- Do not let a model rewrite, interpret or "correct" parsed source text, values or formulas.
- Do not relax the gold parsing dataset to hide an environment difference; re-baseline and report.
- Do not delete named volumes, rotate credentials by editing .env, reset or discard uncommitted
  work, overwrite durable instruction files, or claim checks that were not run.
- Do not commit secrets or expose the development stack publicly.

Last updated: 2026-09-06 by Claude Code.
