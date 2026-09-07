# AI HANDOFF

## Current milestone

**M9 is complete and verified, including a live end-to-end OpenAI run with nothing stubbed.**
Baseline `e85cecc` on branch `main` (M8 `e85cecc`, M7 `5d83197`, M6 `2061000`, M5 `ecb4941`). All M9
work is uncommitted; nothing is staged — the user has not authorized a commit. **M10 has NOT
started.**

## What M9 is

The public Ask experience, connected to the M5–M8 chain without altering any stage in it.

```
question → M5 retrieval → M6 rerank + evidence → M7 gate → M7 draft
        → M8 claim verification → PASS → verified answer + citations
        ↘ any other outcome → typed refusal; no answer text anywhere
```

`POST /api/v1/ask` requires `ask:submit`. `GET /api/v1/conversations[/{id}]` requires
`conversation:read`.

- **The display rule is unrepresentable to violate.** `AskResponse` validates that an answer exists
  **iff** the outcome is `VERIFIED`, that `verified` agrees, and that a refusal carries no claims,
  citations or sources. The stored view enforces the same on read, and two database CHECK
  constraints enforce it underneath — a direct SQL insert of an answer on an unverified turn is
  rejected by PostgreSQL.
- **M9 decides nothing.** It reads the outcome M7 and M8 already reached. `RETRIEVAL_READY` still
  means the corpus may participate in retrieval, not that any question is answerable.
- **Five distinct outcomes**: verified, insufficient evidence, conflicting sources, unverified draft,
  technical failure. An outage is never reported as missing evidence.
- **Enablement is per-contract.** M5–M8 keep `answering_enabled: Literal[False]`; `AskResponse` pins
  `Literal[True]`. `AskConfig.requires_verified_pass` is `Literal[True]` and `stream_answer_tokens`
  is `Literal[False]` — no configuration relaxes either.
- **No confidence figure exists anywhere.**

Key modules: `backend/app/{api/ask,core/ask_config,models/conversations,repositories/conversations,
schemas/ask,services/ask}.py`; `migrations/versions/m9_conversations_*.py`; frontend
`features/ask/{Ask,Answer}.tsx`; `scripts/{smoke_m9,smoke_m9_live}.py`. See ADR-014 and
`docs/architecture/ask.md`, `docs/verification/m9.md`.

## Verified results (2026-09-07)

| Check | Result |
|---|---|
| M9 focused (`MEDRAG_RUN_INTEGRATION=1`) | **64 passed** (43 units + 21 integration) |
| Backend regression, eight fresh-process groups | **659 passed** (179+28+71+148+44+73+52+64) |
| `ruff check` / `format --check` | Passed / 237 files |
| `mypy backend/app` | Success, 172 source files |
| `scripts/check_skills.py` | 9 synchronized pairs |
| `alembic current` / `check` | **`m9_conversations (head)`** / no drift |
| Migration round trip | `head → m5_hybrid_retrieval → head` passed |
| Frontend tests / build | **69 passed** (10 files) / passed |
| Playwright live | **15 passed**, incl. two new Ask specs |
| Docker `--profile app up -d --wait` | 9 services healthy |
| Smokes M1–M5, M6, M7, M8, M9 × 3 fixtures | **PASS ×11** |

`pytest --collect-only` reports 659, so the partition covers the suite exactly. **Never attempt a
monolithic single-process run** — this host produces a native Docling access violation that way.
**Run Playwright only against a drained ingestion queue**; the worker parses at
`MAX_CONCURRENCY: 1` and concurrent regression load makes worker-dependent waits flake.

## Live end-to-end (nothing stubbed)

`scripts/smoke_m9_live.py` generates a synthetic coherent document, uploads it, waits for real
ingestion, then asks through the public endpoint — so M5, M6, M7, the real OpenAI generator, the real
verifier, persistence and citation rendering all run.

Result: **VERIFIED**, `verified: true`, 1 citation resolving to 3 real source elements with a real
bounding box on page 1. **13 656 ms** end to end — drafting 3 794 ms, verification 13 627 ms,
retrieval + rerank + assembly ≈ 200 ms. One passing call proves the integration, not the model.

Non-PASS branches were also exercised live: corpus fixtures return `INSUFFICIENT_EVIDENCE`, and a
probe against the ingested parsing fixtures returned `UNVERIFIED` after the real generator and
verifier ran, the deterministic layer rejecting `NEGATION_REVERSED`, `NUMERIC_MISMATCH` and
`CLAIM_NOT_CITED`.

The parsing fixtures are shaped to exercise the parser and their text is fragmentary, so a verifier
correctly refuses claims drawn from them. **A live PASS needs a document written in coherent prose**
— that is why the live smoke generates its own.

## Persistence

`m9_conversations` (revises `m5_hybrid_retrieval`): `conversations`, `conversation_turns`,
`turn_citations`, all tenant-scoped with composite keys. Stored: question, outcome, answer only when
verified, declared reason codes, model identity, timings, citation identifiers and the exact cited
text. **Not stored**: prompts, provider responses, failed drafts, verifier reasoning, EvidenceSets.

Citation text is stored rather than re-resolved, so a later re-parse cannot silently change what a
stored answer appears to cite.

**The downgrade refuses while conversations exist.** Set `MEDRAG_ALLOW_CONVERSATION_LOSS=1` to
acknowledge the loss — the integration harness sets it on its throwaway schema, which is why the
suite can still prove reversibility.

## Defects found and fixed in M9

1. **Relaxing service-level authorization opened `/retrieval/search` to readers.** The shared
   retrieval, drafting and verification stages must accept a reader's `ask:submit`, and that route
   had no check of its own. It now enforces the scope its docstring already claimed. Every
   diagnostic route needs its own check; the service check is not the boundary.
2. **An unknown conversation id was detected only after a provider call.** Ownership is now resolved
   before the pipeline runs.
3. **A citation opened the citation's first page, not the page its region is on.**
4. **Three obsolete assertions** in `App.test.tsx`, `workspace.spec.ts` and `retrieval.spec.ts` said
   answering was unavailable — the thing M9 changes. Each was rewritten to the narrower invariant
   that still holds, not deleted.
5. **A crude `sk-` substring check** in an M7 test matched the new `ask-outcome` element id; it now
   matches a key shape.

## Provider setup

`MEDRAG_GENERATOR__PROVIDER` / `MEDRAG_GENERATOR__MODEL_ID`, optional `MEDRAG_VERIFIER__*`, and
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. All optional; with none set the pipeline still runs and the
draft stage reports `GENERATION_PROVIDER_UNCONFIGURED`, which surfaces to the user as `FAILED`, not
as missing evidence. Compose delivers keys to the **API container only**. **Never create a `VITE_`
copy.** **The verifier is currently the same model as the generator** (`MEDRAG_VERIFIER__*` unset),
recorded on every turn as `verifier_independent: false`.

## Infrastructure

Alembic head `m9_conversations`. The private `retrieval` service publishes no host port. Rebuild app
images after changing backend or frontend source — code is baked in. Playwright needs
`MEDRAG_DEV_PRINCIPALS` exported from `.env` (single-quoted there — strip the quotes) plus
`MEDRAG_E2E_LIVE=1`. Credentials stay in ignored `.env`; **never print them**.

## If you continue

M9 is done; nothing is outstanding. Get explicit authorization before starting M10. What M9 leaves
open: the verifier is not independent; coverage is low because most questions against the ingested
fixtures abstain; latency is provider-dominated at ~13.7 s and is not an SLO; streaming carries
progress only; region highlighting depends on M2 having recorded a box; and conversations have no
sharing, export, deletion or retention policy.

Do not weaken a check to raise coverage or to make the UI feel faster. An unverified answer shown to
a reader is the failure this entire chain exists to prevent. Preserve M5 analyzer/BM25/RRF/index
semantics, M6 reranking and evidence, the M7 gate and M8 verification. Do not introduce score
thresholds, query rewriting, a calibrated confidence figure, or draft-token streaming.
