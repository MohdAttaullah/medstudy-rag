# AI HANDOFF

## Current milestone

**M6 is complete and verified.** M5 is committed at `ecb4941` on branch `main`. All M6 work is
uncommitted and nothing is staged — the user has not authorized a commit. **M7 has NOT started.**
Ask is disabled, `READY` is unreachable, and every M5 and M6 response carries
`answering_enabled=false`. No generation, sufficiency gate, provider adapter, claim extractor or
verifier exists anywhere in the tree.

## What M6 is

Pinned MedCPT CrossEncoder reranking of the M5 fused pool, deterministic context expansion, and a
bounded, request-scoped EvidenceSet whose every block resolves back to the original document.
The EvidenceSet is **source material for inspection**. It is not an answer, and it is not a claim
that the evidence suffices.

- `ncbi/MedCPT-Cross-Encoder` @ `71caf65d4927987813984f54c284405a13fcca49`, seven files SHA-256
  verified before an offline `local_files_only` / `trust_remote_code=False` / `weights_only=True`
  load. 512-token pairs including special tokens; overflow is **rejected**, never truncated.
- Raw float32 logits, descending, ties on original fused rank then chunk UUID. The score is a
  **ranking diagnostic** — no sigmoid, no confidence, no threshold anywhere.
- Full persisted M3 retrieval text is paired with the M5-normalized query; never the API preview.
  Neither query nor source text is persisted or logged.
- Authorized `POST /api/v1/retrieval/rerank` (`retrieval:search`, hybrid only). Corpus identity is
  rechecked after retrieval, after inference and after expansion hydration; drift raises
  `CONTEXT_SOURCE_LINEAGE_MISMATCH`. **No fallback to the M5 result on reranker failure.**
- Defaults: 20 candidates (max 40), 5 anchors, 1 sibling each side, parent 512 tokens, neighbour
  256, 3 expansions per anchor; budget 4096 tokens / 20 blocks / 1024 tokens per block.
- M5 is untouched: encoder, analyzer, BM25 `k1`/`b`, RRF equation and constant, lane weights and
  40/40 budgets are unchanged. The M6 pool is a separately versioned query-side policy
  (`retrieval-m6-pool-v1`). `git diff` over the M5 retrieval core is empty.

Key modules: `backend/app/{reranking,evidence,core/reranking_config,schemas/reranking,
services/evidence,repositories/evidence,evaluation/{reranking,evidence}}`; frontend
`features/retrieval/EvidenceInspector.tsx`; `scripts/{evaluate_reranking,
compare_reranking_environments,benchmark_m6,provision_reranker_model,smoke_m6}`.
See ADR-011, `docs/architecture/{reranking,context-expansion}.md` and `docs/verification/m6.md`.

## Provenance fixes (do not regress these)

1. **Captionless figures.** An original figure whose chunk has zero-length spans and no caption was
   silently dropped by assembly. A `visual_only` predicate now admits it and deduplication keys on
   `(document_version_id, artifact_id)`. `FIGURE_CAPTION` is emitted only when a caption span
   exists. Nothing generates a description of the image.
2. **Multi-chunk questions.** `EvidenceSource`/`EvidenceBlock` carry `source_chunk_ids`; the
   repository resolves every chunk sharing the question's `question_id` in the same tenant and
   chunk run. The Evidence Inspector links **each** contributing chunk, and `smoke_m6` resolves
   `/chunks/{id}/sources` for every one of them.

Covered by `test_visual_only_original_remains_inspectable`,
`test_atomic_question_retains_all_contributing_chunk_ids` and
`links every contributing chunk of a multi-chunk source record`.

## Verified results (2026-09-07)

| Check | Result |
|---|---|
| M6 focused backend (`MEDRAG_RUN_INTEGRATION=1`) | **43 passed**, 49.00 s |
| Backend regression, five disjoint fresh-process groups | **468 passed** (178 + 28 + 71 + 148 + 43) |
| `ruff check` / `format --check` (`backend workers scripts`) | Passed / 187 files |
| `mypy backend/app` | Success, 137 source files |
| `scripts/check_skills.py` | 9 synchronized pairs |
| `alembic current` / `check` | `m5_hybrid_retrieval (head)` / no drift, **no M6 migration** |
| Frontend tests / build | **45 passed** (7 files) / passed |
| Playwright live | **11 passed**, incl. `reranking.spec.ts` |
| Docker `--profile app up -d --wait` (all images rebuilt) | 9 services healthy |
| Smokes M1–M5, then M6 × table/formula/figure/question-bank | **PASS** ×9 |

Reranking (frozen pool `eb19a7f80d65…`, 83 chunks, 24 queries): MRR 1.0000 → 1.0000, nDCG@5
0.9809 → **0.9967**, Recall@3 0.9773 → **1.0000**, Recall@1 **0.7500 unchanged**. Pools 10 and 20
identical; 30 and 40 marginally worse and slower. **FIRST_STAGE_MISS 0, RERANKER_REGRESSION 0**;
the only class recorded is `CORPUS_LACKS_EVIDENCE` ×2 for the two deliberate negatives.

Context expansion: 0/0 → 1/1 siblings raises source-element coverage 0.8833 → **0.9333** and
character coverage 0.8856 → 0.8970 with noise unchanged at 0.0333. **Open gaps: `table`
(0.667) and `figure` (0.667) at every budget.** Do not relabel gold to hide them.

Host vs Linux: 838/2400 scores bit-identical, max Δ 5.7220e-05, **24/24 complete order agreement at
every pool size**, all ranking metrics equal, context evaluation byte-identical. Bit equality is
**not** claimed.

Warm (API container, pool 20): M5 first stage 70.2 ms, hydration 233.4, **reranking 1056.6**,
expansion 8.5, assembly 1.1, total 1370.2 ms. Cold model load ~21 s. Private `retrieval` container
holds **858.5 MiB** with both models warm; API 195.4 MiB, dispatcher 168.4 MiB (no torch).

**Measured and deliberately not acted on:** the raw CrossEncoder logit separates positives
(min 4.2376) from negatives (max −14.2521) where RRF does not (0.031319 vs 0.031746). That is an
observation on 24 synthetic queries. **It is not a threshold. Do not make it one.** Evidence
sufficiency is M7 and needs evidence this benchmark cannot supply.

## Backend regression practice on this host

A single monolithic `pytest backend/tests` **crashed** with a Windows native access violation inside
Docling at 15%, with ~600 MB host memory free. It produced no passing full-suite result and none is
claimed. Run the suite as the five disjoint groups above in **fresh processes** so each model
runtime is released first; they exactly partition all 468 tests. The crash did not reproduce as a
deterministic test failure — the 28 Docling integration tests pass in isolation.

## Infrastructure

Alembic head `m5_hybrid_retrieval`; M6 adds no schema and no ingestion state. Successful jobs end at
`RETRIEVAL_READY`, which is **not** answer readiness — verified live:
`ck_document_versions_m1_never_searchable` is `CHECK ((NOT searchable))` and `m1_job_guard` has no
transition targeting `READY`.

The private `retrieval` service keeps Query Encoder and CrossEncoder warm and **publishes no host
port** (unreachable from the host; reachable only over the compose network). Its healthcheck
requires both `/health/ready` and `/health/reranker`. It has no database, tenant concept or
authorization — the API owns all access decisions. Article Encoder stays an ingestion workload.
Caches: `.local/models/{embeddings,reranking}` on the host; `query-models` and `reranker-models`
named volumes in compose. Rebuild the app images after changing backend or frontend source — code
is baked in, not mounted.

Credentials stay in ignored `.env` and `.local/dev-access.txt`; **never print them**. Playwright
needs `MEDRAG_DEV_PRINCIPALS` exported from `.env` (single-quoted there — strip the quotes) plus
`MEDRAG_E2E_LIVE=1`. Use explicit UTF-8 in Python file reads/writes; PowerShell redirection can
produce UTF-16 logs.

## If you continue

M6 is done; nothing is outstanding. Before starting M7, get explicit authorization. The open work
M6 leaves behind is the `table` and `figure` context-expansion coverage gaps, and the fact that
nothing in this benchmark calibrates the candidate pool, the budgets or any threshold.

Synthetic metrics are engineering checks, not clinical validation. Preserve M5 analyzer, BM25, RRF
and index semantics. Do not introduce score thresholds, query rewriting, generation or medical
answering.
