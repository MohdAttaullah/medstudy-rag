# AI HANDOFF

## Current milestone

**M7 is complete and verified.** M6 is committed at `2061000` on branch `main`. All M7 work is
uncommitted and nothing is staged — the user has not authorized a commit. **M8 has NOT started:**
no claim extraction, no entailment verification, no repair attempt, no streaming, no Ask experience.
`answering_enabled` and `verified` are both `Literal[False]` and `READY` remains unreachable.

## What M7 is

An Evidence Sufficiency Gate between the M6 EvidenceSet and any provider call, and grounded
generation that can only be reached through it.

```
question → M5 retrieval → M6 rerank + evidence → GATE → SUFFICIENT? → one provider call → draft
                                                      ↘ INSUFFICIENT / CONFLICTING → abstention
```

The provider is **constructed after** the decision, so no path exists on which a model runs and the
gate is consulted afterwards. `POST /api/v1/retrieval/draft` requires `retrieval:search` **and**
`generation:draft`, and always returns the decision whether or not a draft was produced.

- **No score is sufficiency.** `retrieval_scores_permitted` is `Literal[False]`, no policy field is
  a float, and `gate.py` never references a lane or reranker score — all three asserted by test.
  M6's logit-separation observation was deliberately **not** made a threshold.
- **Signals**, not opinions: supporting anchors (expansion context is not independent support),
  independent document versions, non-assessment sources, authority levels, table header rows,
  formula artifact, visual interpretation, budget omissions, partial fragments, retrieval warnings,
  conflicts. Each returned with its requirement and whether it was satisfied. **No percentage.**
- **Question kinds** classified deterministically (`question-kind-v1`) from a fixed cue table over
  the user's own analyzer terms, falling back to anchor chunk types. No model, no rewriting.
  Tokenization uses a locally declared analyzer config, *not* the tenant's active index policy, so
  rebuilding a lexical index cannot change what the gate decides.
- **Figure questions always abstain** — no vision path is approved and both adapters refuse
  `analyze_image`. **Table questions abstain without header rows.** These are the M6 context gaps
  surfacing honestly rather than being reconstructed by a generator.
- **Assessment material is never sufficient alone.** A question bank records what an examiner
  marked, not what the corpus establishes; a high rerank position does not change that.
- **Conflicts are preserved, never resolved.** Two narrow deterministic detectors, both
  over-triggering by design. Precedence `CONFLICTING` > `INSUFFICIENT` > `SUFFICIENT`, with every
  triggered reason code reported regardless.
- **Generator input** is the grounding policy, the question and the EvidenceSet — no corpus handle,
  no tool, no web search, no rank or score. The prompt states pretrained knowledge is not evidence.
- **Citations** are validated against the evidence actually supplied. That is a contract check;
  whether a cited block *supports* its sentence is M8.
- **Every failure abstains.** `max_attempts=1`, `fallback_policy=NONE`, and no `except
  GenerationError` in the service — asserted by test.

Key modules: `backend/app/{sufficiency/{gate,model,question,conflicts},generation/{errors,
grounding/model,prompts/grounded,citations/validate,providers/{base,openai,anthropic,fake,factory}},
core/generation_config,schemas/generation,services/generation,evaluation/sufficiency}`; frontend
`features/retrieval/DraftInspector.tsx`; `scripts/{evaluate_sufficiency,smoke_m7}`.
See ADR-012, `docs/architecture/{sufficiency,generation}.md`, `docs/verification/m7.md`.

## Verified results (2026-09-07)

| Check | Result |
|---|---|
| M7 focused (`MEDRAG_RUN_INTEGRATION=1`) | **65 passed** (54 units + 11 integration) |
| Backend regression, six disjoint fresh-process groups | **532 passed** (178+28+71+148+43+64) |
| `ruff check` / `format --check` (`backend workers scripts`) | Passed / 208 files |
| `mypy backend/app` | Success, 154 source files |
| `scripts/check_skills.py` | 9 synchronized pairs |
| `alembic current` / `check` | `m5_hybrid_retrieval (head)` / no drift, **no M7 migration** |
| Frontend tests / build | **49 passed** (8 files) / passed |
| Playwright live | **12 passed**, incl. `sufficiency.spec.ts` |
| Docker `--profile app up -d --wait` | 9 services healthy |
| Smokes M1–M5, M6, M7 × table/figure/question-bank | **PASS ×9** |

`pytest --collect-only` reports 532, so the partition covers the suite with no overlap or gap. **No
monolithic single-process run was attempted** — this host has already produced a native Docling
access violation under memory pressure that way. Keep using the six-group fresh-process partition.

Sufficiency evaluation (`m7-sufficiency-gold-v1`, 14 cases over the frozen M5 gold corpus):
agreement **1.0000**, **false allows 0**, unnecessary abstentions 0, abstention rate 0.7143. Each
case abstains for its correct specific reason. **This is weak evidence:** the cases were written
alongside the gate that decides them, in the same session. It shows internal consistency, not
generalization, and nothing clinical.

Generation contract: attempted on 4 of 14, suppressed on 10; schema validity, citation validity and
invented-citation rejection all 1.0; evidence-id hallucination 0.0; no rank or score leaked to the
provider.

Live, against real uploads with no provider configured — table → `TABLE_STRUCTURE_INCOMPLETE`,
figure → `VISUAL_INTERPRETATION_UNAVAILABLE`, question-bank → `ASSESSMENT_ONLY_EVIDENCE`. All three
abstained before any provider was reached. Gate cost 0.18–2.83 ms of a 394–440 ms pipeline.

## Defects found and fixed in M7

1. **API container would not boot** when a generator env var was declared but empty — compose
   passes `""`, which failed `ModelSelection` validation and took the whole API down merely because
   no provider account was configured. Fixed with a `field_validator` treating an all-empty
   selection as unconfigured. A prior host check wrongly suggested this was safe; PowerShell's
   `$env:X=""` *removes* a variable rather than emptying it, so it never reproduced the condition.
2. **Vendor base URLs were in `core/generation_config.py`,** outside the adapters. Caught by the
   isolation test written for exactly that; moved into the adapters.
3. **The signals table overflowed a 390 px viewport.** The app already had a `.table-scroll`
   convention I had not used. Caught by the browser test.

## Provider setup

`MEDRAG_GENERATOR__PROVIDER` (`openai`|`anthropic`), `MEDRAG_GENERATOR__MODEL_ID`, and
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` (the `MEDRAG_`-prefixed forms also work). All optional and
empty by default: with none set the gate still runs and the draft stage reports
`GENERATION_PROVIDER_UNCONFIGURED`, which is a declared unavailable state, **not** a fallback to
some default model. Compose delivers them to the **API container only**. **Never create a `VITE_`
copy** — anything `VITE_` is compiled into the browser bundle and would publish the key; a test
asserts none exists. Keys stay out of responses, logs and metric labels.

**No live provider call has ever been made from this repository.** Adapters are verified against
mocked httpx transports at the wire level and the draft path with a deterministic double. Do not
claim live-provider verification without running one.

## Infrastructure

Alembic head `m5_hybrid_retrieval`; M7 adds no schema, no migration and no ingestion state. Nothing
about a question, decision or draft is persisted. The private `retrieval` service still publishes no
host port. Rebuild app images after changing backend or frontend source — code is baked in.
Playwright needs `MEDRAG_DEV_PRINCIPALS` exported from `.env` (single-quoted there — strip the
quotes) plus `MEDRAG_E2E_LIVE=1`. Credentials stay in ignored `.env` and `.local/dev-access.txt`;
**never print them**.

## If you continue

M7 is done; nothing is outstanding. Get explicit authorization before starting M8. What M7 leaves
open: nothing in the gate is calibrated and no held-out evaluation exists; the conflict detectors
over-trigger and miss prose-level contradictions; every figure question abstains; and answer
correctness and claim entailment are entirely unmeasured — that is precisely M8's job.

Do not weaken an abstention to raise coverage. An unnecessary abstention costs coverage; a false
allow emits an unsupported medical answer, and the two are not interchangeable. Preserve M5
analyzer, BM25, RRF and index semantics, and M6 reranking and evidence semantics. Do not introduce
score thresholds, query rewriting or medical answering.
