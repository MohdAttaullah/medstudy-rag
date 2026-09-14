# ADR-023: Figure dependence is a property of the question, not of what ranked

Status: accepted. Post-M12 acceptance hardening; no milestone was created. Narrows the
implementation of ADR-012's figure rule to what ADR-012 actually says. Vision remains disabled and
M8 is unchanged.

## Context

ADR-012 states the rule precisely:

> *"A figure **question** is `INSUFFICIENT` in every case, because M7 approves no vision-analysis
> path and a caption — or the structural label M3 writes for a captionless figure — records that a
> figure exists rather than what it shows."*

The implementation read wider than that. After the question's own cues, `classify` fell through to
a structural test over the retrieved anchors:

```python
kinds = {block.chunk_type for block in anchors}
if kinds & FIGURE_CHUNKS:
    return "FIGURE_DEPENDENT"
```

A set intersection over **all** anchors, so one `FIGURE_CONTEXT` among five made the whole question
visual. Since `vision_analysis_available` is `Literal[False]`, `FIGURE_DEPENDENT` is permanently
unsatisfiable, and the question was refused.

Measured on the real 932-page Medical Microbiology corpus, across the 26-question acceptance set:

| Measurement | Result |
|---|---|
| Questions classified `FIGURE_DEPENDENT` | **11 of 26** |
| ... because the question's own words asked for a figure | **0** |
| ... because a figure chunk merely ranked | **11** |
| ... with exactly one figure anchor | 10 |
| ... with at least one readable textual anchor | 10 |
| ... blocked **solely** by `visual_interpretation` | **11 of 11** |
| Figure blocks in all 26 EvidenceSets | 19 |
| ... carrying no text at all | **0** |

Not one abstention was caused by a question about a picture. The clearest case: *"What structural
features make bacteria prokaryotic organisms?"* was refused while its own textual anchor read
*"prokaryotic organisms — simple unicellular organisms with no nuclear membrane…"*, because a
57-character caption, *"FIGURE 12-1 Major features of prokaryotes and eukaryotes."*, ranked fifth.
Several of the offending "figures" were table captions that M2 had classified as FIGURE artifacts.

ADR-013 records M8 learning this exact lesson already:

> *"Table, formula and figure checks key off what the claim **cites**, not off its single type
> label. An earlier version keyed them off the label and let a table without header rows through…"*

The gate never learned it.

## What FIGURE_CONTEXT actually is

A `FIGURE_CONTEXT` chunk is the figure's **caption and adjacent prose**, gathered from
`figure_neighbour_elements`. The image is not in the chunk. Measured text length across the 19 real
blocks: min 10, median 34, max 946 characters — none empty. M3 already marks the textless case at
ingestion with `CHUNK_FIGURE_NO_TEXT`.

So a figure block is normally *readable source text*. What it cannot do is tell you what the
picture depicts.

## Decision 1 — the question decides, with one structural floor

```
FIGURE_DEPENDENT  ⟺  (asked & FIGURE_CUES)
                  ∨  (anchors ∧ ∀a ∈ anchors: visual(a) ∧ ¬readable(a))

visual(a)    ≡  a.chunk_type ∈ FIGURE_CHUNKS ∨ a.requires_visual_evidence
readable(a)  ≡  bool(a.text.strip())
```

No score, no threshold, no caption-length rule, no count or proportion of figure anchors. The floor
fires only when *every* anchor is a figure carrying no text — there is then genuinely nothing to
read without pixels, and the question fails closed.

`visual()` deliberately reuses the same disjunction M8 applies in
`verification/deterministic.py`. If the gate and the claim checker disagreed about what counts as
visual, one of them would be wrong about every block they disagreed on.

The table, formula and assessment structural fallbacks are untouched. Removing the figure branch
from that chain means a question retrieving a figure *and* a table part is now correctly
`TABLE_DEPENDENT` instead of being classified away as visual.

## Decision 2 — the cue table gains in-image deixis, and one word is refused

Added: `arrow`, `arrows`, `arrowhead`, `arrowheads`, `label`, `labels`, `labelled`, `labeled`,
`panel`, `panels`, `inset`, `circled`, `indicated`, `pictured`. *"What structure is indicated by
the arrow?"* names no figure and matched nothing before, yet it cannot be answered without reading
the picture.

Deliberately **not** added: `imaging`. *"How does pulmonary cryptococcosis present on imaging?"*
asks what the literature describes about radiological appearance, which the text answers. Treating
it as a request to read a picture would refuse a legitimately textual question — the same over-reach
this ADR exists to remove.

`CLASSIFIER_VERSION` moves `question-kind-v1` → `question-kind-v2`, because the semantics changed
and a decision record must not silently mean something new.

## Decision 3 — M8 remains the guarantee, and is unchanged

Not one line of M8 changed. A claim citing a figure block still fails `VISUAL_INTERPRETATION_REQUIRED`,
deterministically, whatever the question was classified as:

```python
if claim.claim_type == "VISUAL_DEPENDENT" or any(
    b.requires_visual_evidence or b.chunk_type in FIGURE_CHUNKS for b in cited
):
    codes.append("VISUAL_INTERPRETATION_REQUIRED")
```

That is what makes relaxing the gate safe rather than merely permissive. A genuinely visual
question that slips the cues reaches generation, produces claims citing the figure block, and those
claims fail — the question ends `UNVERIFIED`, not answered. A claim citing text *and* a figure
together still fails, so mixed citation is not a way round the rule. `test_figure_verification_regression_units.py`
pins all of it so a future change to `classify` cannot quietly remove the thing standing behind it.

Captions therefore remain readable context for the generator — rendered with
`note: original figure not interpreted; caption text only` — while never being able to support a
released claim on their own.

## Decision 4 — the gate reports, and still refuses

`require_visual_interpretation` is untouched: a genuine `FIGURE_DEPENDENT` question still abstains
before generation with `VISUAL_INTERPRETATION_UNAVAILABLE`. Added is a non-blocking
`figure_anchors_present` signal counting visual anchors, so a misclassification in either direction
— a visual verdict with no figure anchors, or an answered question carrying several — is visible in
the decision record rather than only in the outcome.

## Consequences

All 11 previously figure-classified questions were blocked solely by `visual_interpretation`, so
all 11 now reach the gate under their real kind. Ordinary questions whose answer sits in their
textual anchors can be answered; out-of-corpus ones reach the provider and abstain semantically via
ADR-022's declination rather than as technical failures.

**A predicted cost, stated plainly:** because M8 fails a claim citing text *and* a figure together,
some of these will land on `UNVERIFIED` rather than `VERIFIED`. That is still an improvement — the
refusal becomes claim-accurate instead of question-wide — but it is not eleven recovered answers,
and it should not be reported as one. Changing that conjunction is M8 policy and was out of scope.

## Limitations

The structural floor has **no positive instance in this corpus**: zero of 19 figure blocks were
textless. Its correctness rests on reasoning and unit tests, not on observation of a real case.

A separate pre-existing defect was found and deliberately left alone: `"cell"` is a `TABLE_CUES`
member, so *"Describe prokaryotic cell structure."* classifies `TABLE_DEPENDENT`. It behaved that
way before this change — question cues have always preceded the structural fallback — and altering
the table vocabulary is outside this decision.
