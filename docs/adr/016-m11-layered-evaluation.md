# ADR-016: Layered evaluation, governed datasets, and no single accuracy number

Status: Accepted for M11. Baseline `e0ee0ad` (committed M10). Preserves ADRs 010–015. No production
component changed; M11 adds measurement, not behaviour.

## There is no overall score, and that is the decision

The obvious thing to build is one number that goes up. It would be the most-quoted output of this
repository and the least meaningful: parsing fidelity, Recall@5 and a false-PASS count measure
different things on different datasets, and any weighting of them is arbitrary. Worse, the
aggregate would move for reasons nobody could explain, and a regression in the one quantity that
actually matters — an unsupported medical claim released as verified — would be diluted by
fourteen figures that do not matter nearly as much.

So eleven layers are measured separately and reported separately, and a failed answer is
attributed to the stage that caused it. The aggregate report is a table of contents.

## Attribution runs upstream

A question that fails because retrieval never found the evidence *also* looks like an
insufficiency: the gate refuses because there is nothing to answer from, which is the gate working
correctly. Attributing that refusal to the gate would credit it with a failure retrieval caused,
and would make the report conclude that the system abstains too much when what it actually does is
retrieve too little — which would send the next piece of work to the wrong place entirely.

`attribute()` therefore returns the earliest layer that declared a code, never the last one to
notice. `FIRST_STAGE_MISS` stays distinct from `RERANKER_REGRESSION`; an outage stays distinct from
an empty corpus; a correct refusal is not counted as a defect.

The taxonomy reuses the repository's own reason codes and invents none, because a second name for
one condition is how a taxonomy starts disagreeing with the code it describes. Tests assert that
every member of the real `ReasonCode` literals is classified, so a new code cannot silently become
an anonymous failure.

## Datasets carry what they are allowed to prove

Almost every gold file here was written during the milestone it measures. That is a reasonable way
to build a pipeline and a poor way to claim it generalises: a fixture authored alongside the policy
it scores shows that the implementation does what its author intended, not that it works on cases
nobody anticipated. Once a number reaches a table that distinction is invisible, so it is recorded
in code and printed next to the figure rather than buried in a caveats section.

Independence is separate from provenance, because a synthetic dataset can still be held out and a
real-source-derived one can still be implementation-adjacent. Content is fingerprinted and the
manifest fingerprints the set, so an edited gold label cannot pass unnoticed; a correction requires
a new version.

**On the word "held out."** M11 adds 25 end-to-end cases written after `e0ee0ad`. The claim they
support is narrow and exact: every policy they exercise was committed before the cases existed, so
no case influenced a threshold. They are not a held-out sample of a real population, they are
synthetic, and the engineer who wrote them ran them. Claiming more from a fixture set that scores
well would be precisely the failure this classification exists to prevent, so the dataset states
its own limits in its own notice.

Two cases were revised after their first run, and it matters which kind of revision that was. One
had wording that shifted the conflict detector's label window so the intended comparison never
happened; one named a deterministic reason code where the system emits a semantic one. Both were
errors in the *case*, and no policy, threshold or prompt was touched at any point — which is the
line that separates held-out discipline from theatre.

## Gates say how much authority they have

A false PASS is zero because the architecture exists to make it zero, and any non-zero value is a
defect whatever the dataset size. A Recall@5 from 25 synthetic cases is not that. Reporting both as
"the gates passed" would lend the first one's authority to the second, so every gate is labelled
`HARD_SAFETY_INVARIANT`, `ENGINEERING_REGRESSION_THRESHOLD`, `OBSERVATIONAL_METRIC` or
`UNCALIBRATED`, and the label travels into the report.

`UNCALIBRATED` is a real outcome, not an omission. Answer correctness is the clearest case: it
needs an expert-reviewed dataset that does not exist here, so it is recorded as unmeasured rather
than approximated by something convenient. Inventing a medical-quality threshold from a small
synthetic corpus would be worse than admitting the gap.

**A safety invariant that was not measured has not been upheld.** The verdict refuses to pass on
that basis. A first implementation of one gate mapped a missing layer to a failure instead of to
"not measured" — the inverse mistake, and equally wrong; a test now pins both directions.

## Offline by default, live only when asked

The default evaluation calls no provider, needs no API key and touches neither PostgreSQL nor
Qdrant, and every hard safety invariant is measured in that mode — the things this system must
never do are decided by code the harness can exercise directly. An evaluation that needed
infrastructure or a paid key would stop being run, and an evaluation nobody runs measures nothing.
An integration test makes any HTTP request raise and asserts the offline run still passes.

`--live` is never implied by another flag and refuses without a configured generator. It makes a
small fixed number of calls and records what it is: evidence the integration works, not evidence
about the model.

## Consequences, including the uncomfortable ones

The verifier is the same model as the generator, so verification is not independent, and an error
the generator makes is one the verifier is disposed to repeat. This is recorded on every run rather
than mentioned once. The comparison against an independent verifier was not run because no second
approved model is configured, and M11 does not add a paid provider integration to manufacture one.

Cost cannot be reported: the M7 provider adapter does not read the response usage block, so no
token counts exist. Reporting zero would read as "this was free." Capturing usage is an M7 adapter
change and was deliberately not made under an evaluation milestone.

EvidenceSet coverage is 0.933 under the configured policy, not 1.0. That gap is real and stays
visible; the sweep's better-scoring policies describe configurations nobody is running, so the
configured one is what gets quoted.

On this corpus hybrid retrieval scores identically to dense alone at Recall@5. That is a fact about
a 5-document synthetic corpus, not a finding about hybrid retrieval, and it is explicitly not a
reason to remove a lane.

No new retrieval or generation component entered the production path, no prompt was changed to let
a model answer from pretrained knowledge when retrieval missed, and no verifier was weakened to
raise the answer rate. Those changes would all have improved a number while making the system
worse, which is the trade this milestone exists to refuse.
