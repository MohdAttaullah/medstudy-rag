# AI HANDOFF

## Current milestone

**M8 is complete and verified, including one live OpenAI run.** Baseline `9965c4d` on branch `main`
(M7 `5d83197`, M6 `2061000`, M5 `ecb4941`). All M8 work is uncommitted; nothing is staged — the user
has not authorized a commit. **M9 has NOT started:** no conversations, no citations UI, no
source-page highlighting, no streaming, and the production Ask experience is disabled.

## What M8 is

Claim-level verification between the M7 grounded draft and any released answer.

```
question → M5 retrieval → M6 rerank + evidence → M7 gate → M7 draft
        → M8 claim extraction → deterministic checks → semantic verification → contradiction
        → PASS (verified answer) / REGENERATE_ONCE (one repair, fully re-verified) / ABSTAIN
```

`POST /api/v1/retrieval/answer` requires `retrieval:search` + `generation:draft` +
`generation:verify`. An `INSUFFICIENT` or `CONFLICTING` gate still abstains; M8 never generates to
overcome missing evidence.

- **Claims come from the answer text, not the generator's declarations.** A sentence the generator
  omitted from its own claim list is extracted, found uncited and failed `CLAIM_NOT_CITED`. This is
  the milestone's most important structural decision.
- **Deterministic checks run first and bind.** Citation identity in *this* EvidenceSet, provenance
  resolution, `(value, unit)` agreement, negation polarity, certainty overstatement, canonical table
  headers, formula artifacts, figure abstention. A model is never asked to overrule one — a test
  asserts the verifier recorded **zero calls** on a wrong-dose claim it would have approved.
- **The verifier sees one claim and only its cited evidence.** No corpus, retrieval, web search,
  rank or score. Strict verdict schema; prose is never the decision. Unknown evidence, malformed
  output and provider failure all abstain.
- **Contradiction covers uncited retained evidence** — the shape a generator creates by citing only
  the source that agrees with it — plus reference-vs-reference disagreement and
  assessment-vs-reference. Rank never breaks a tie.
- **One repair, capped by type**, same evidence only, then the entire flow runs again. A second
  failure abstains.
- **`verified=true` exists in one place**: `VerifiedAnswer`, built only behind a PASS.
  `answering_enabled` stays false. Failed verdicts are retained so an abstention is auditable.

Key modules: `backend/app/verification/{model,claims,deterministic,verifier,contradiction,engine}.py`,
`core/verification_config.py`, `schemas/verification.py`, `services/verification.py`,
`generation/prompts/repair.py`, `evaluation/verification.py`; frontend
`features/retrieval/VerificationInspector.tsx`;
`scripts/{evaluate_verification,smoke_m8,smoke_m8_live}.py`. See ADR-013 and
`docs/architecture/verification.md`, `docs/verification/m8.md`.

## Verified results (2026-09-07)

| Check | Result |
|---|---|
| M8 focused (`MEDRAG_RUN_INTEGRATION=1`) | **52 passed** (42 units + 10 integration) |
| Backend regression, seven disjoint fresh-process groups | **595 passed** (179+28+71+148+44+73+52) |
| `ruff check` / `format --check` (`backend workers scripts`) | Passed / 225 files |
| `mypy backend/app` | Success, 166 source files |
| `scripts/check_skills.py` | 9 synchronized pairs |
| `alembic current` / `check` | `m5_hybrid_retrieval (head)` / no drift, **no M8 migration** |
| Frontend tests / build | **54 passed** (9 files) / passed |
| Playwright live | **13 passed**, incl. `verification.spec.ts` |
| Docker `--profile app up -d --wait` | 9 services healthy |
| Smokes M1–M5, M6, M7, M8 × table/figure/question-bank | **PASS ×10** |

`pytest --collect-only` reports 595, so the partition covers the suite exactly. **Never attempt a
monolithic single-process run** — this host has produced a native Docling access violation that way.

Evaluation (`m8-verification-gold-v1`, 19 cases): **false PASS 0**, agreement 1.0000, abstention
rate 0.7895, unsupported-claim detection 1.0, reason-code precision 1.0, repair cap respected.
**Weak evidence:** the cases were written alongside the checks that decide them, in the same session.

**Browser flakiness, recorded not hidden.** Two full Playwright runs each failed one or two
worker-dependent waits while a backend regression suite ran concurrently (worker parses at
`MAX_CONCURRENCY: 1`). Each spec passed in isolation and the suite passed 13/13 on a drained queue.
Host contention, not a product regression. Run Playwright when the ingestion queue is idle.

## Live provider verification

2026-09-07, `scripts/smoke_m8_live.py` inside the API container, synthetic non-sensitive evidence:
gate SUFFICIENT → real generator → claim extraction → deterministic checks → real verifier →
**PASS**, `verified=true`, 1 of 1 material claims supported, **repair_count 1** (the first draft
failed, the single repair succeeded and was re-verified in full). Generation 4139 ms, verification
2244 ms, repair 2001 ms; extraction 0.6 ms. Prints no key, prompt or provider error body.

**The verifier is currently the same model as the generator** (`gpt-5.6-sol`), because
`MEDRAG_VERIFIER__*` is unset — recorded as `independent_of_generator: false` in every spec and
warned in the inspector. Set `MEDRAG_VERIFIER__PROVIDER` / `MEDRAG_VERIFIER__MODEL_ID` for genuine
independence. Anthropic remains verified against mocked transports only.

## Defects found and fixed in M8

The first evaluation run produced two false PASSes and one unnecessary abstention. All three were
real, and were fixed rather than relabelled:

1. **A numeric claim drawn from a table skipped the table check.** `classify()` returns one type and
   tests `NUMERIC` before `TABLE_DERIVED`, so a headerless table passed. `check_structured_evidence`
   now inspects the blocks the claim *cites*, not its type label.
2. **`check_negation` fired on claims containing no negation**, because no single sentence of a
   structured block met the overlap bar and the fallback reported reversal rather than agreement.
   It now requires a genuine polarity disagreement.
3. **A fixture did not encode the conflict it named** — its key and reference were not lexically
   about the same subject. Corrected; the underlying limitation (lexically distant contradictions
   are missed) is recorded in the report.

Also fixed: the fake verifier did not mirror the real adapter's malformed-output handling, and a
repaired draft citing invented evidence was reported as a phantom verifier fault instead of a
citation failure.

## Provider setup

`MEDRAG_GENERATOR__PROVIDER` / `MEDRAG_GENERATOR__MODEL_ID`, optional `MEDRAG_VERIFIER__*`, and
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. All optional: with none set the gate still runs and the
draft stage reports `GENERATION_PROVIDER_UNCONFIGURED` — a declared unavailable state, not a
fallback. Compose delivers keys to the **API container only**. **Never create a `VITE_` copy.** An
empty `ANTHROPIC_API_KEY` does not prevent startup.

## Infrastructure

Alembic head `m5_hybrid_retrieval`; M8 adds no schema, migration or ingestion state, and persists
nothing about a question, claim, verdict or answer. The private `retrieval` service publishes no
host port. Rebuild app images after changing backend or frontend source — code is baked in.
Playwright needs `MEDRAG_DEV_PRINCIPALS` exported from `.env` (single-quoted there — strip the
quotes) plus `MEDRAG_E2E_LIVE=1`. Credentials stay in ignored `.env`; **never print them**.

## If you continue

M8 is done; nothing is outstanding. Get explicit authorization before starting M9. What M8 leaves
open: the verifier is not independent in this environment; deterministic checks are lexical and miss
contradictions expressed in non-overlapping words; claim extraction is rule-based and will decompose
unusual punctuation imperfectly; negation scope is approximated at sentence level; visually
dependent claims always abstain; and no threshold anywhere is calibrated.

Do not weaken a check to raise coverage. A false PASS emits a wrong medical answer; an unnecessary
abstention emits nothing, and the two are not interchangeable. Preserve M5 analyzer/BM25/RRF/index
semantics, M6 reranking and evidence semantics, and the M7 gate. Do not introduce score thresholds,
query rewriting or a calibrated confidence figure.
