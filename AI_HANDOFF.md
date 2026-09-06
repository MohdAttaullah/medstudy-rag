# AI HANDOFF

## Current milestone and status

**M5 — biomedical hybrid retrieval: COMPLETE and verified locally.** M0–M4 remain verified.
**M6 has not been started.**

Successful jobs execute
UPLOADED → VALIDATING → QUEUED → PARSING → NORMALIZING → ENRICHING → READY_FOR_CHUNKING →
CHUNKING → VALIDATING_CHUNKS → READY_FOR_EMBEDDING → EMBEDDING → INDEXING → VERIFYING_INDEX →
READY_FOR_RETRIEVAL → SPARSE_INDEXING → VERIFYING_SPARSE_INDEX → **RETRIEVAL_READY**.

RETRIEVAL_READY means both retrieval lanes verified over the *same* chunk dataset, so this corpus
version can take part in hybrid retrieval and its candidates resolve to real sources. **It does not
mean the corpus is medically answerable.** No reranking, context expansion, evidence sufficiency
gate, grounding or generation exists. READY stays unreachable in both the application transition
table and the database guard, every version stays database-constrained unsearchable, Ask is
disabled, and every retrieval response carries `answering_enabled: false`.

## Completed implementation (M5)

- **MedCPT Query Encoder** pinned by revision `d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc`, with both
  the weight and tokenizer SHA-256 verified before load. CLS pooling, 768 dimensions, unnormalized,
  DOT, 64-token maximum — the released query-side representation. A *different checkpoint* from
  M4's article encoder; the model id is `Literal`-pinned so configuration cannot swap them, and
  `Settings` refuses to build if the two policies disagree on the vector space.
- Query preparation is deterministic only: NFKC, control characters, typographic punctuation,
  whitespace. **No rewriting, no HyDE, no paraphrase, no synonym or abbreviation expansion** — the
  analyzer's `expansion` is `Literal["NONE"]` behind a database CHECK. Over-long queries are
  rejected with `QUERY_TOO_LONG`, never truncated. The bounded query-vector cache is keyed by
  encoder semantics plus a hash and stores no query text.
- **PostgreSQL BM25** with the Lucene IDF, over a versioned biomedical analyzer that keeps
  `HLA-B27`, `CYP3A4`, `Na+/K+-ATPase`, `HbA1c`, `IL-6`, `mg/kg`, `7.5%` intact, emits case-exact
  terms in their own key space, and disables stopwords. Postings store raw term frequencies and
  lengths, so `k1`/`b` are runtime-safe and IDF is scoped to the tenant's active corpus only.
- Durable sparse lifecycle mirroring M4: outbox message kind `SPARSE_INDEX`, fenced lease,
  reconciliation against the dense lane's own recorded chunk set, activation only after
  verification, previous active index preserved when a replacement fails, completed indexes
  immutable, activation timestamp written once, findings append-only.
- **RRF** `sum(weight/(k+rank))`, k=60, weights 1.0, deterministic chunk-id ties. Raw lane scores
  are carried as diagnostics and never added. Modes `DENSE_ONLY`, `BM25_ONLY`, `HYBRID_RRF`;
  `BM25_ONLY` performs no model call at all.
- **Fail-closed corpus alignment**: a version is searchable only with an active VERIFIED dense run
  and an active VERIFIED sparse index on the same chunk run, at RETRIEVAL_READY, unarchived. Lane
  disagreement, mixed embedding versions and mixed analyzer versions all fail the whole query.
  Default `degradation_policy = FAIL_CLOSED`.
- Tenant comes only from the authenticated principal; both lanes filter server-side; hydration
  re-scopes every candidate against tenant and resolved corpus.
- Retrieval API + status/inspection/reindex APIs, and a **Retrieval Inspector** UI with mode
  selection, lane comparison, execution trace and candidate → chunk → source-page navigation. No
  answer, confidence or dense vector is exposed anywhere.
- Query encoding runs in its own internal `retrieval` container with an offline provisioned
  `query-models` volume, so the API and dispatcher images carry no torch.
- Gold retrieval dataset (83 synthetic chunks, 22 positive / 2 negative / 1 boundary case, 20
  categories) and an offline evaluation harness reporting Recall@K, MRR, nDCG, precision,
  per-category results, per-query diagnostics, failure classification, negative-case score
  separation and query-boundary behaviour.

Modules: `backend/app/{core/retrieval_config,retrieval/*,retrieval_service,models/retrieval,
repositories/retrieval,schemas/retrieval,api/retrieval,services/{retrieval,sparse_index},
observability/retrieval,evaluation/retrieval}`; `frontend/src/features/retrieval`;
`frontend/e2e/retrieval.spec.ts`; `migrations/versions/m5_hybrid_retrieval`;
`scripts/{provision_query_model,evaluate_retrieval,compare_query_environments,smoke_m5}`.
See [M5 report](docs/verification/m5.md), [retrieval](docs/architecture/retrieval.md),
[sparse retrieval](docs/architecture/sparse-retrieval.md) and
[ADR-010](docs/adr/010-m5-hybrid-retrieval.md).

## Git and files

Branch: **main**. Committed baseline **d50521b** (M4). Earlier: **6cca864** (M3), **0d2085a** (M2),
**bb2bfa0** (M1).

**All M5 work is uncommitted** in the working tree — **34 modified tracked files and 35 untracked
paths**: M5 modules, tests, fixtures, migration, scripts, frontend feature and docs. Nothing is
staged. No
history was rewritten, reset or force-pushed; no named volume was deleted; no other agent's work
was discarded. `.env`, `.local/` and model caches remain ignored; no secret and no model weight is
in the tree. **The milestone commit decision is the user's.**

Host-account Git commands may still need the scoped override because sandbox identity initialized
Git: `git -c safe.directory=D:/Projects/RAG/Proj_1_DoctorDocuments status --short --branch`.

## Verification executed

| Check | Final result |
|---|---|
| Complete backend suite, `MEDRAG_RUN_INTEGRATION=1` | **425 passed** (6m44s) |
| M5 focused suite (units + integration) | **148 passed** |
| `test_m5_units.py` / `test_m5_integration.py` | 103 / 45 passed |
| ruff check / ruff format --check | Passed / 167 files formatted |
| mypy backend/app | Success, 124 source files |
| scripts/check_skills.py | 9 synchronized pairs |
| alembic current / check | `m5_hybrid_retrieval (head)` / no drift |
| M5 → M4 → M5 migration round trip | Passed |
| npm --prefix frontend test / build | **40 passed** (6 files) / passed |
| Playwright, `MEDRAG_E2E_LIVE=1` | **10 passed** (1.2m), incl. 2 new retrieval specs |
| docker compose --profile app up -d --wait | 9 services healthy incl. `retrieval` |
| Query service readiness | ready, pinned revision, offline cache |
| smoke_m1 / m2 / m3 / m4 / m5 | **PASS / PASS / PASS / PASS / PASS** |
| Retrieval evaluation, host and Linux | Metrics identical; see below |
| Query-vector host vs Linux comparison | 0/11 byte identical, max Δ 1.073e-06, 11/11 ranking |

Two boundary assertions moved deliberately, each with its reason in the test: the liveness
milestone string, and `test_answering_states_remain_unreachable_after_m5`.

Two **test** defects were found and fixed during final verification, neither a product defect.
An ambiguous Playwright selector in the M4 spec was made exact (`getByText('768')` matched three
elements once the point list grew). And `test_hybrid_search_returns_hydrated_candidates_with_full_provenance`
asserted the target chunk would be *fused* rank 1, which RRF does not guarantee and should not —
a chunk ranked second by both lanes rightly outranks one ranked first by dense alone. It now
asserts dense rank 1, which is what the test was actually proving. Both are stricter than before.
No test was disabled or weakened.

## Measured retrieval baseline (synthetic corpus — not clinical validation)

Dataset `retrieval-gold-m5-v1`, SHA-256 `375a438834869826…`, 83 chunks, 22 positive cases.

| Lane | R@1 | R@3 | R@5 | MRR | nDCG@5 | nDCG@10 |
|---|---|---|---|---|---|---|
| Dense | 0.7500 | 0.9773 | 1.0000 | 1.0000 | 0.9788 | 0.9788 |
| BM25 | 0.6591 | 0.8712 | 0.9545 | 0.9269 | 0.9120 | 0.9272 |
| Hybrid RRF | 0.7500 | 0.9773 | 1.0000 | 1.0000 | 0.9809 | 0.9809 |

Identical on host and in the Linux runtime. Recall@10/@20 are 1.000 everywhere and are
**structurally uninformative** at this corpus size. The only category that separates the lanes is
`lexically_weak`: dense 1.000, BM25 0.000, hybrid 1.000.

**The fused RRF score is not a relevance signal.** Measured: a negative query's top fused score
(0.031746) exceeds the weakest positive one (0.031319). Lane scores do separate on this dataset,
but on 22 positives and 2 negatives that is an observation, not a threshold, and none is drawn.

Parameter sweeps over candidate budget (10/20/40/60), RRF k (10/20/60/100) and fusion weights moved
no metric meaningfully. **Nothing was tuned**; the configured values remain declared seeds.

Reports in ignored `.local/m5-retrieval-{host,linux}.{json,md}`,
`.local/m5-query-{host,linux}.json`, `.local/m5-query-comparison.json`, `.local/m5-smoke.json`.

## Migrations and infrastructure

Applied: `0dd8e0dcb035`, `m1_event_order`, `m2_document_parsing`, `m3_hierarchical_chunks`,
`m4_embeddings_and_index`, **`m5_hybrid_retrieval` (head)**. M5 adds seven retrieval tables and the
outbox `sparse_index_id`, and is the first revision that must **widen** the status columns
(`VERIFYING_SPARSE_INDEX` is 22 characters); that requires dropping and recreating the M3
`m3_job_cancelled` trigger, which is declared `AFTER UPDATE OF status`. Its downgrade refuses to
run while any job holds an M5 state and deletes `SPARSE_INDEX` outbox rows first.

Running: API 127.0.0.1:8000; frontend 127.0.0.1:5173; PostgreSQL 5432; Redis 6379; MinIO 9000/9001;
Qdrant 6333/6334; worker; dispatcher; and **retrieval** (query encoder, internal only, port not
published). Model caches: `parser-models` (downloaded on first use), `embedding-models` (article
encoder, provisioned), `query-models` (query encoder, provisioned). Provision the query cache with:

```
docker compose run --rm --no-deps --entrypoint python retrieval \
  /repo/scripts/provision_query_model.py --cache /home/medrag/models/embeddings
```

Generated credentials stay in ignored `.env`; UI keys in `.local/dev-access.txt` — do not print
them. `MEDRAG_DEV_PRINCIPALS` is single-quoted, so strip the quotes when exporting to Playwright.
Use `uv run --cache-dir .uv-cache --extra parsing --extra embedding --env-file .env` with
`MEDRAG_RUN_INTEGRATION=1` for integration runs. Sequentialize heavy model checks when memory is
tight (host has 16 GB).

## Decisions, limitations and blockers

ADR-001 … ADR-009 remain accepted. **ADR-010** records the query encoder, the relational BM25
choice, RRF, and the corpus-alignment policy.

**Determinism, three separate claims, measured separately** (mirroring M4's article-encoder
finding): same host repeated encoding is byte-identical; batched versus single is numerically
equivalent but not byte-identical; host versus Linux is **0/11 byte identical**, worst delta
1.073e-06, with **11/11 top-10 ranking agreement**. Cross-platform bit equality is not claimed.
Additionally measured: every retrieval metric is identical between host and Linux, so that
numerical difference moves no ranking on this dataset.

Other limitations: retrieval quality is measured only on a synthetic 83-chunk corpus and cannot
calibrate budgets, the RRF constant or weights; question-bank and answer-key material is
retrievable and stays labelled but conflicts are not resolved (that is M7); the abbreviation case
passes because the dense lane carries it, not because abbreviations are handled generally; BM25
loads bounded postings into Python and refuses a query above `max_scanned_postings` rather than
truncating; mixed embedding or analyzer versions fail closed rather than being merged; Qdrant HNSW
parameters are defaults and unmeasured; dense latency is CPU-bound with no GPU, quantization or
ONNX path, and a cold query costs ~2.6 s against ~46 ms warm. M2–M4 limitations are unchanged.
This is local development, not clinical validation or compliance certification. Production startup
remains explicitly rejected.

No remaining M5 blocker.

## Next exact task

Stop. Wait for the user's M6 authorization. Then begin:
**M6 — biomedical cross-encoder reranking, parent/neighbour/context expansion, final evidence-set
construction, and reranking-quality evaluation.**

M6 consumes the `CandidateSet` M5 produces: fused candidates already carry chunk id, both lane
ranks and scores, hydrated provenance and a preview, which is exactly a cross-encoder's input.
Read this handoff, [the M5 report](docs/verification/m5.md), ADR-003 and ADR-010, and the
retrieval-quality and rag-evaluation skills first. Rerank only the fused candidates, never the raw
corpus. Expansion must preserve provenance and deduplicate overlap. Do not enable Ask, generation,
grounding or answering.

## Do not do

- Do not expose partially processed versions or enable medical answering.
- Do not present a retrievable corpus as answerable, or RETRIEVAL_READY as clinical readiness.
- Do not use the fused RRF score as an evidence-sufficiency signal; it was measured not to separate
  answerable from unanswerable queries.
- Do not tune BM25 `k1`/`b`, the RRF constant, the weights, the analyzer or the candidate budgets to
  improve a synthetic score. Fix real defects and report before/after.
- Do not quote synthetic Recall@5 = 1.000 as evidence of medical reliability, and do not quote
  Recall@10/@20 at all on this corpus.
- Do not claim cross-machine bit-identical query vectors; the measured deviation is above.
- Do not add synonym or abbreviation expansion, query rewriting or HyDE without an ADR and its own
  evaluation.
- Do not promote question keys, captions or generated metadata to authoritative medical evidence.
- Do not let a model rewrite, interpret or "correct" parsed source text, values or formulas.
- Do not relax the parsing, chunking, embedding or retrieval gold datasets to hide a difference;
  re-baseline and report.
- Do not delete named volumes, rotate credentials by editing .env, reset or discard uncommitted
  work, overwrite durable instruction files, or claim checks that were not run.
- Do not commit secrets, model weights, or expose Qdrant or the retrieval service publicly.

Last updated: 2026-09-06 by Claude Code.
