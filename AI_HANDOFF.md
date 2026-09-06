# AI HANDOFF

## Current milestone and status

M3 - Structure-aware hierarchical chunking with complete source provenance, deterministic chunk
identity, versioned rechunking and chunk-quality validation: COMPLETE and verified locally.
M0, M1 and M2 remain verified.

Successful jobs execute
UPLOADED -> VALIDATING -> QUEUED -> PARSING -> NORMALIZING -> ENRICHING -> READY_FOR_CHUNKING ->
CHUNKING -> VALIDATING_CHUNKS -> READY_FOR_EMBEDDING.
READY_FOR_EMBEDDING means parsed, chunked and validated, not retrievable: no embedding, vector or
index exists and every version stays database-constrained unsearchable. Medical answering is
disabled. **M4 has not started.**

## Completed implementation (M3)

- Chunking consumes only the **active** ParseRun and its persisted rows. The raw Docling artifact
  is a debugging record, not a second chunking input. The builder receives frozen contracts and can
  reach neither the database, the parser library nor object storage.
- Durable versioned ChunkRun: chunker name/version, policy version plus a SHA-256 fingerprint of
  the whole frozen policy, config snapshot, pinned tokenizer identity, a separate SHA-256
  fingerprint of the normalized input, metrics, fenced lease and safe error. One active dataset and
  one pending-or-running run per version, enforced by partial unique indexes and check constraints.
- Parent (default 1280 tokens) and child (default 384) chunks over declared ancestry and
  deterministic numbered headings, splitting by section, paragraph, list, sentence and only then by
  token offsets into the normalized source. Decoded token text is never used to rebuild source.
- Tables bypass the text splitter: canonical cells retained, row-span-aware row groups, identical
  repeated headers on every part, linked footnotes attached, flagged continuations never merged, an
  invalid grid failing rather than being repaired.
- Formulas atomic and verbatim with only parser-linked neighbours. Figures carry caption, linked
  text and the artifact id with no visual reasoning. Repeated page margins are excluded from
  chunking and remain fully present in the parse run.
- Question objects with options atomic to the question, explicit source answers preserved, absent
  answers left absent (an inferred answer is unstorable by check constraint), explicit-vs-inferred
  structure recorded, authority metadata carried, ambiguity routed to review.
- Complete span-level provenance: chunk -> source elements with offsets and role -> pages ->
  artifacts -> active parse run -> version -> document -> original PDF. Composite foreign keys keep
  every mapping inside the same tenant, version, parse run and chunk run.
- Deterministic chunk-quality layer with persisted findings and PASS / PASS_WITH_WARNINGS /
  NEEDS_REVIEW / FAIL. Source coverage is measured by **text**, not by element identity: text no
  chunk carries is CRITICAL, text only a parent still carries after a split is an ERROR, whitespace
  between spans is not loss.
- Worker pipeline over the existing outbox with a distinct CHUNKING kind: idempotent delivery,
  cancellation, fenced leases, dispatcher sweeping, receipt-window recovery, declared retryability
  and an explicit permissioned rechunk. Phase events are emitted from the point work begins.
- Completed datasets are immutable by trigger; superseding a parse run or cancelling a job
  deactivates the datasets built from it.
- Tenant-authorized chunk inspection APIs, a chunk summary on document details, a Chunk Inspector
  with question and table views and source-page navigation, real chunk stages in Operations, and
  DB-derived chunk series on /metrics with bounded labels.
- Offline chunk construction evaluation harness over 14 synthetic normalized fixtures, separate
  from parsing evaluation and from RAG evaluation.

Modules: backend/app/{api,core,evaluation,ingestion/chunking,models,repositories,schemas,services};
workers/{celery_app,dispatcher}; frontend/src/features/chunking;
migrations/versions/m3_hierarchical_chunks*; scripts/{create_chunk_fixtures,evaluate_chunking,
smoke_m3}.
See [M3 report](docs/verification/m3.md), [document chunking](docs/architecture/document-chunking.md)
and [ADR-008](docs/adr/008-m3-hierarchical-chunking.md).

## Git and files

Branch: main. Commits: **0d2085a** ("feat: complete M2 structured document parsing and provenance")
and **bb2bfa0** ("feat: complete M1 document ingestion control plane").

All M3 work is **uncommitted** in the working tree; nothing is staged. No history was rewritten,
reset or force-pushed, and no prior agent's work was discarded. The declined Codex command was not
replayed; every file it would have touched was inspected and completed deliberately. `.env`,
`.local`, caches, dependencies, build outputs and browser artifacts remain ignored; no secrets are
in the tree. **The milestone commit decision is the user's.**

Host-account Git commands may still need the scoped override because sandbox identity initialized
Git: `git -c safe.directory=D:/Projects/RAG/Proj_1_DoctorDocuments status --short --branch`.
No global Git configuration was changed.

## Verification executed

Commands ran from the repository root against the running development stack.

| Check | Final result |
|---|---|
| MEDRAG_RUN_INTEGRATION=1 pytest -q (as found, before changes) | 2 failed, 189 passed |
| MEDRAG_RUN_INTEGRATION=1 pytest -q (final) | 203 passed (22 upstream warnings) |
| pytest backend/tests/test_m3_units.py -q | 46 passed |
| MEDRAG_RUN_INTEGRATION=1 pytest backend/tests/test_m3_integration.py -q | 15 passed |
| ruff check / ruff format --check backend workers scripts | Passed, 118 files |
| mypy backend/app | Passed, 88 source files |
| python scripts/check_skills.py | 9 synchronized pairs passed |
| alembic current / alembic check | m3_hierarchical_chunks (head) / no drift |
| alembic downgrade m2_document_parsing -> upgrade head -> check | Passed |
| npm --prefix frontend test | 29 passed (4 files) |
| npm --prefix frontend run build | TypeScript and Vite passed |
| MEDRAG_E2E_LIVE=1 PLAYWRIGHT_CHANNEL=chrome playwright test | 6 passed |
| docker compose config --quiet | Passed |
| docker compose --profile app up -d --build --wait | All services healthy |
| uv run --env-file .env python scripts/smoke_m2.py | PASS |
| uv run --env-file .env python scripts/smoke_m3.py | PASS (containerised worker parsed and chunked a real upload) |
| python scripts/evaluate_chunking.py (host) | 14/14 cases, 94/94 checks |
| evaluate_chunking.py in the Linux worker image | 14/14 cases, 94/94 checks, identical dataset hashes |

Two stale M2 assertions were updated for M3 reality (READY_FOR_CHUNKING -> CHUNKING is now legal,
and the unreachable-state test now asserts the M4 states); two frontend assertions were updated
because the product copy legitimately changed. No test was disabled or weakened.

## Migrations and infrastructure

Applied: `0dd8e0dcb035`, `m1_event_order`, `m2_document_parsing`, `m3_hierarchical_chunks`
(current head). The M3 revision adds eight chunk tables with their indexes, constraints and
immutability/invalidation triggers, widens the status vocabulary and column width for
VALIDATING_CHUNKS and READY_FOR_EMBEDDING, replaces the transition guard with the M3 graph
including the rechunk edge, and adds `kind`/`chunk_run_id` to outbox messages. Its downgrade
refuses to run while any row still holds an M3-only status rather than silently rewriting it; the
integration teardown cancels such jobs first, which is the documented operator action.

Running: API 127.0.0.1:8000; frontend 127.0.0.1:5173; PostgreSQL 5432; Redis 6379; MinIO 9000/9001;
Qdrant 6333/6334 (no collection, no corpus); worker and outbox dispatcher. The worker builds from
`infrastructure/docker/worker.Dockerfile` and is ~2.8 GB; it carries both the parser and the
chunker, and the bundled tokenizer ships inside `backend/app/ingestion/chunking/assets/medcpt/`.
Parser weights live in the `parser-models` named volume; the chunker needs no network at all.

Note: `scripts/` and `backend/tests/` are not copied into the worker image. To run an evaluation
inside the Linux image, bind-mount the repository:
`docker run --rm -v <repo>:/repo -w /repo -e PYTHONPATH=/repo/backend medical-rag-worker:latest
python /repo/scripts/evaluate_chunking.py`.

Generated credentials remain in ignored `.env`; UI keys are in `.local/dev-access.txt` — do not
print them. Note that `MEDRAG_DEV_PRINCIPALS` in `.env` is single-quoted; strip the quotes when
exporting it to Playwright. Browser and smoke tests leave synthetic publications in the local
tenant. No user medical corpus was imported.

## Decisions, limitations and blockers

ADR-001 through ADR-007 remain accepted. **ADR-008** records deterministic hierarchical chunks over
normalized source, with an outcome note on the two rules that tightened during implementation.

**Determinism: two different claims.** Identical normalized input + identical policy + identical
tokenizer produce identical chunks and hashes; this was verified on the Windows host and in the
Linux worker image, which produced byte-identical dataset hashes for all 14 fixtures. That is *not*
a claim that parsing the same PDF on both platforms yields the same normalized input — M2 measured
that it does not, for borderline semantic labels (page header/footer, caption association, formula
detection, list grouping), and the parsing gold dataset remains baselined per environment
(host 9/9, container 4/9). M3 is deliberately built so those labels cannot decide a boundary or a
content hash. Do not relax either gold dataset to hide an environment difference, and do not claim
environment-independent parsing.

Other limitations: token targets and ChunkThresholds are uncalibrated defaults chosen against
synthetic fixtures and express no quality or accuracy; whether these are good retrieval units is
unanswerable until embeddings, an index and retrieval evaluation exist; question recognition is
conservative single-pattern matching and will not recognise many real layouts (unrecognised
material stays ordinary text rather than becoming a wrong question object); there is no
review-approval workflow; no retention or cleanup of superseded chunk runs; no memory sandbox or
per-tenant quota; worker in-process chunk timing is not scrapeable. Parsing limitations from M2 are
unchanged. This remains local development: no production OIDC, credential lifecycle, AV scanning,
TLS, managed secrets, backup/retention/purge or installed telemetry pipeline. None of this is
clinical validation or compliance certification. Production startup remains explicitly rejected.

No remaining M3 blocker.

## Next exact task

Stop. Wait for the user's M4 authorization. Then begin:
**M4 - Biomedical embedding generation, versioned vector representations, Qdrant indexing, atomic
index activation, and embedding/index-quality validation.**

Read AGENTS.md, this handoff, [document chunking](docs/architecture/document-chunking.md),
[data model](docs/architecture/data-model.md), ADR-002/003/007/008 and the retrieval-quality and
rag-evaluation skills. Consume only the **active** ChunkRun (`is_active`) and its persisted chunks;
embed `retrieval_text` and carry `chunk_hash`, source spans, pages and artifact relations into the
vector payload so a citation still resolves to an exact source region. Add the EMBEDDING and
INDEXING edges to both the application transition table and the database guard in a reviewed
migration; they are deliberately absent today. Index activation must be an atomic manifest switch
owned by PostgreSQL, verified against expected IDs and counts before any reader can see it. Add
embedding/index-quality evaluation separate from parsing evaluation, chunk evaluation and RAG
evaluation. Do not skip ahead into retrieval fusion, reranking or answering.

## Do not do

- Do not expose partially processed versions or enable medical answering.
- Do not fabricate parse, chunk or index progress, accuracy percentages or clinical accuracy.
- Do not promote question keys, captions or generated metadata to authoritative medical evidence.
- Do not infer a question answer the source does not state.
- Do not let a model rewrite, interpret or "correct" parsed source text, values or formulas.
- Do not relax the parsing or chunking gold datasets to hide a difference; re-baseline and report.
- Do not delete named volumes, rotate credentials by editing .env, reset or discard uncommitted
  work, overwrite durable instruction files, or claim checks that were not run.
- Do not commit secrets or expose the development stack publicly.

Last updated: 2026-09-06 by Claude Code.
