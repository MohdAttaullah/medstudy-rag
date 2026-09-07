# AI HANDOFF

## Current milestone

M11 COMPLETE and verified. Baseline main `e0ee0ad` (committed M10), clean at start. All M11 work is
uncommitted; nothing staged; no history rewritten. M0–M10 were not restarted. **M12 has NOT
started.**

**No production behaviour changed in M11.** It adds measurement only: no retrieval, reranking,
generation or verification code path was modified, and no prompt, threshold or policy value was
touched. See `docs/verification/m11.md` and `docs/adr/016-m11-layered-evaluation.md`.

## Architecture

`backend/app/evaluation/` gains `taxonomy.py` (failure attribution), `datasets.py` (governance +
fingerprinted manifest), `manifest.py` (run identity), `gates.py` (quality gates), `end_to_end.py`
(held-out layer), `harness.py` (orchestration), `report.py` (rendering) and `live.py` (opt-in
provider probe). The eight pre-existing per-layer evaluators are orchestrated, not replaced;
`evaluate_parsing.py` and `evaluate_embeddings.py` gained `--json` so they could be.

Eleven layers are measured and reported separately. **There is deliberately no overall accuracy
score.** Failure attribution returns the *earliest* layer that declared a code, so a retrieval miss
is never reported as an over-eager gate, and `FIRST_STAGE_MISS` stays distinct from
`RERANKER_REGRESSION`.

## Entry points

```bash
uv run python scripts/evaluate_m11.py                                     # offline (CI default)
uv run --extra embedding python scripts/evaluate_m11.py --include-models  # + model-backed layers
uv run --extra embedding python scripts/evaluate_m11.py --live            # + opt-in provider calls
```

Offline needs no API key, no provider call, no PostgreSQL and no Qdrant, and measures **all seven
hard safety invariants**. An integration test makes any `httpx` request raise and asserts the
offline run still passes. `--live` is never implied by another flag and costs money. Exits non-zero
if a gate fails or a hard safety invariant went unmeasured. Artifacts: `docs/evals/m11/`.

## Datasets

Nine registered, content-fingerprinted; manifest `47af1814b27f8198`. **7 implementation-adjacent,
1 frozen regression, 1 held out, 0 expert-reviewed.** The independence class is printed next to the
numbers in every report section. A test fails if a gold file exists that nobody registered.

`m11-heldout-v1` is 25 end-to-end cases written after `e0ee0ad`, held out only in the sense that
every policy they exercise was frozen before they existed. Not a sample of a real population, not
clinician-reviewed. Two cases and two attribution annotations were corrected after their first run;
**no policy or threshold was changed at any point.**

## Results

All eleven layers run. Parsing 9/142 checks, chunking 14/94, embedding 9/180 — zero failures each.
Retrieval hybrid Recall@5 1.000, candidate-pool recall 1.000, `FIRST_STAGE_MISS` 0.000. Reranking
nDCG@5 0.981 → 0.997 with 0 regressions. EvidenceSet coverage 0.933 under the configured policy.
Sufficiency agreement 1.000 with **0 false allows**. Verification agreement 1.000 with **0 false
PASS**. Held-out end-to-end: gate, outcome and attribution agreement all 1.000, abstention 0.760,
**0 answers on unanswerable questions**. Security 13 checks, 0 violations. Live probe: 3 real calls,
2 suppressed before the provider, 0 invented citations, median 4163 ms.

**Quality gates: 7/7 hard safety invariants upheld, no failed gates.** A safety invariant that was
not measured does not pass.

Backend regression, fresh process per group: 179+28+71+148+44+73+52+64+48+**92** = **799 passed**,
matching `--collect-only` exactly. ruff clean, format 259, mypy 187 files, skills 9 pairs. Frontend
75 passed + build. Playwright 17 on a drained queue. Docker 9 services healthy. **No M11 migration**
— Alembic head stays `m10_configuration`, which is correct for a file-based evaluation milestone.

## Known failure categories and limitations

* The verifier is the same model as the generator; the independence experiment was **not run**
  because no second approved model is configured. Recorded on every run.
* **Cost is unreportable**: the M7 provider adapter does not read the response `usage` block, so no
  token counts exist. Clearest follow-up work M11 identified — it is an M7 adapter change and was
  deliberately not made under an evaluation milestone.
* The M7 conflict detector compares values only when the four-word label window preceding the
  number matches between sources; disagreements phrased differently are missed. Measured, frozen
  into the held-out set as `h-conflict-label-mismatch`, not fixed here.
* EvidenceSet coverage is 0.933, not 1.0.
* One post-repair unsupported claim (correctly abstained, not released).
* Hybrid retrieval shows no Recall@5 advantage over dense on a 5-document corpus — a property of
  the corpus, not a finding about hybrid retrieval.
* Latency figures are Windows development-host measurements, not SLOs. Timer semantics: layer
  timings are additive; M5–M9 stage timings are nested and must not be summed.
* **Synthetic engineering evaluation is not clinical validation.** No clinical claim is made.

## Next work

M12 is production hardening and has not been started. Do not begin it without an explicit request.

Operational notes: run browser and live verification on a drained worker queue. Playwright's pinned
browser was installed manually from Google's official chrome-for-testing artifact because
Playwright's own download host returns `400` from this network. Credentials remain in ignored
`.env`; never print them. Use `uv run --cache-dir .uv-cache --extra parsing --extra embedding
--env-file .env` with `MEDRAG_RUN_INTEGRATION=1`. No automatic commit.
