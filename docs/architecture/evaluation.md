# M11 evaluation framework

Implementation: `backend/app/evaluation/`. Entry point: `scripts/evaluate_m11.py`. Decision:
[ADR-016](../adr/016-m11-layered-evaluation.md). Results: [M11 report](../verification/m11.md).

**M11 engineering evaluation is not clinical validation.** Every dataset is synthetic, none is
expert-reviewed, and nothing here is evidence about clinical performance.

## Layers stay separate

Eleven layers are measured independently: parsing, chunking, embedding/index, retrieval,
reranking, EvidenceSet, sufficiency, generation, verification, end-to-end Ask, and security.

There is deliberately **no overall accuracy score**. A parsing fidelity figure, a Recall@5 and a
false-PASS count are not commensurable; averaging them yields a number whose movement nobody can
explain while hiding the one quantity that matters underneath fifteen that do not. The aggregate
report is a table of contents, not a verdict.

A layer that cannot run is recorded as `NOT_RUN` with its reason, never silently omitted, and that
distinction is carried into the gates.

## Failure attribution is upstream

`taxonomy.py` maps every declared reason code to the layer that owns it, using the repository's own
codes — no second name is invented for a condition the system already declares. A test asserts that
every member of the real `ReasonCode` literals in `sufficiency/model.py` and `verification/model.py`
is classified, so M7 or M8 cannot add a code that reports as anonymous.

`attribute()` returns the **earliest** layer that declared a code. A question that fails because
retrieval never found the evidence will also look like an insufficiency, since the gate correctly
refuses when there is nothing to answer from; attributing that to the gate would credit the gate
with a failure retrieval caused, and would make the report say the system abstains too often when
what it really does is retrieve too little. `FIRST_STAGE_MISS` therefore stays distinct from
`RERANKER_REGRESSION`, and an end-to-end failure keeps its upstream attribution instead of
collapsing into a generic `FAILED`.

Three further distinctions are kept explicit: `INFRASTRUCTURE` codes (an outage is never a
statement about the corpus), `CORRECT_REFUSAL` codes (a corpus that genuinely lacks an answer
producing an abstention is the system working), and `SUCCESS` codes (never counted as a failure).

## Dataset governance

`datasets.py` records what each dataset is and therefore what it can be evidence *of*. Two
independent axes:

**Provenance** — `SYNTHETIC` or `REAL_SOURCE_DERIVED`.

**Independence** — what may be concluded:

| Class | Meaning |
|---|---|
| `IMPLEMENTATION_ADJACENT` | Authored while implementing the behaviour it measures. Evidence that the implementation matches its author's intent; **not** evidence of generalisation. |
| `FROZEN_REGRESSION` | Frozen after its milestone. Evidence that behaviour has not changed; not that it was correct. |
| `HELD_OUT` | Authored after the policy under test was committed, and evaluated without tuning that policy afterwards. |

**Review** — `UNREVIEWED`, `ENGINEER_REVIEWED` or `EXPERT_REVIEWED`. Nothing in this repository is
expert-reviewed, and the manifest reports that count explicitly rather than leaving it implied.

Each dataset is fingerprinted by content, and the manifest fingerprints the whole set, so a
silently edited gold label changes the manifest fingerprint and every report quoting it. A gold
correction requires a new dataset version. `test_every_gold_file_in_the_repository_is_governed`
fails if a gold file exists that nobody registered.

The independence label travels *with the numbers* into every report section, because "98%" means
something entirely different on a fixture written alongside the code than on a case the policy
never saw.

## The held-out set, and the limits of that word

`backend/tests/fixtures/evaluation/heldout.json` holds 25 end-to-end cases authored in M11 against
the pipeline committed at `e0ee0ad`. It is held out in the one sense this repository can honestly
support: **every policy it exercises was frozen before the cases existed**, so no case could have
influenced a threshold.

It is *not* a held-out sample of any real population, it was authored by the same engineer who ran
it, and no clinician reviewed it. The dataset says so in its own notice.

Categories covered: ordinary factual, multi-hop, table-derived, table fragment without headers,
formula-derived, figure requiring visual interpretation, question-bank-only evidence, answer-key
conflict, agreeing authorities, disagreeing authorities, outdated edition, unreviewed source
contradicting a guideline, no answer in corpus, ambiguous, incomplete context, evidence-budget
omission, numeric mismatch, negation reversal, uncited claim, invented citation, verifier
rejection, verifier outage, successful repair, provider outage, and one documented detector
limitation.

## Quality gates carry their own authority

`gates.py` labels every gate, because a failure does not mean the same thing in each case:

| Kind | Meaning |
|---|---|
| `HARD_SAFETY_INVARIANT` | Must hold on any dataset. Zero-tolerance, never traded against coverage. |
| `ENGINEERING_REGRESSION_THRESHOLD` | A bound from measured behaviour on a frozen dataset. Detects change; says nothing about medical quality. |
| `OBSERVATIONAL_METRIC` | Recorded and watched, no bound asserted. |
| `UNCALIBRATED` | No defensible threshold exists on this data. Reported without a bound, deliberately not a gate. |

**A safety invariant that was not measured has not been upheld.** `evaluate_all` refuses to call a
run passed on that basis, so a report can never say "no failures" about a gate that never ran.

Answer correctness is `UNCALIBRATED` and is not measured: establishing it needs an expert-reviewed
dataset this repository does not have. Inventing a medical-quality threshold from a small synthetic
corpus would be worse than admitting the gap.

## Configuration-aware results

Every run records the git commit and dirty state, the environment, the M10 configuration revision,
the fingerprint of every policy that can move a number, model identity and revisions, and the
dataset manifest fingerprint. Two runs are comparable only if these agree.

Model *identity* is configuration and is recorded; credentials are recorded as **presence only**.
No key, prompt or document text reaches an artifact.

`verifier_independent_of_generator` is recorded on every run because, with one model doing both
jobs, an error the generator makes is one the verifier is disposed to repeat. It is the largest
standing caveat on every verification figure.

## Offline and live

```bash
uv run python scripts/evaluate_m11.py                                    # offline (default)
uv run --extra embedding python scripts/evaluate_m11.py --include-models # + model-backed layers
uv run --extra embedding python scripts/evaluate_m11.py --live           # + opt-in provider calls
```

**Offline is the default and is what CI runs**: no API key, no provider call, no PostgreSQL, no
Qdrant. Every hard safety invariant is measured in that mode, because the things this architecture
must never do are decided by code the harness exercises directly. An integration test monkeypatches
`httpx` to fail on any request and asserts the offline run still passes.

`--live` is never implied by another flag. It refuses to run without a configured generator, makes
a small fixed number of calls over synthetic questions, and records that this demonstrates the
integration works — not that the model is reliable.

Model-backed layers (parsing, embedding, retrieval, reranking) need local weights and are skipped
by default so the standard run stays fast and hermetic.

## Artifacts

Written to `docs/evals/m11/`: `m11-report.json` (full aggregate), `m11-cases.jsonl` (one reviewable
row per case across every layer) and `m11-report.md` (human-readable, layered).

The per-case rows are shaped for a future medical reviewer: question, expected answerability,
evidence used, system outcome, verified answer where one exists, citations, and failure
classification. They deliberately carry no provider reasoning or chain of thought — a reviewer needs
the decision and its evidence, not the model's monologue.

The entry point exits non-zero when a gate fails **or** a hard safety invariant went unmeasured.

## What M11 deliberately did not do

No new retrieval or generation component entered the production path. No BGE-M3, ColBERT, GraphRAG,
HyDE, query rewriting or additional provider was introduced. No prompt was changed to make a model
answer from pretrained knowledge when retrieval missed evidence, and no verifier was weakened to
raise the answer rate: a retrieval miss is reported as a retrieval miss.

No migration was created. Evaluation output is file-based, and inventing durable relational state
for it would have been schema churn for its own sake.
