# M7 evidence sufficiency

The gate runs between the M6 EvidenceSet and any provider call. It answers one question — may
generation be attempted at all — and returns a typed decision that can be re-read rather than
trusted. `SUFFICIENT` is the only status that permits generation.

## Statuses

- `SUFFICIENT` — the evidence meets every structural requirement for this kind of question and no
  conflict was detected. Generation may be attempted.
- `INSUFFICIENT` — at least one requirement is unmet. A typed abstention is returned with the
  reason codes and the missing requirements. Abstention is an intended successful outcome.
- `CONFLICTING` — the evidence already contains an unresolved disagreement. All competing blocks
  are preserved and returned. No source is chosen and rank never breaks the tie.

Precedence is `CONFLICTING` > `INSUFFICIENT` > `SUFFICIENT`; every triggered reason code from both
evaluations is reported regardless, so precedence hides nothing.

## Signals

Read from the structure of the EvidenceSet, never from a score. `supporting_blocks` counts ranked
anchors only — expanded context supports its anchor and is not independent support.
`independent_sources` counts distinct document versions, because two chapters of one book are not
two sources. Also evaluated: `non_assessment_sources`, `authority_levels`, `table_structure`,
`formula_source`, `visual_interpretation`, `budget_omissions`, `incomplete_context_blocks`,
`retrieval_warnings` and `evidence_conflicts`. Each is returned with the requirement it was
compared against and whether it was satisfied.

**No retrieval, BM25, dense, RRF or CrossEncoder score participates.** `retrieval_scores_permitted`
is pinned `Literal[False]`, no policy field is a float, and a test asserts the gate module never
references a score attribute. See [ADR-012](../adr/012-m7-sufficiency-and-grounded-generation.md).

## Reason codes

Supporting: `SUPPORTED_BY_SOURCE_EVIDENCE`, `SUPPORTED_BY_INDEPENDENT_SOURCES`,
`SUPPORTED_BY_NON_ASSESSMENT_SOURCE`, `REQUIRED_ARTIFACT_PRESENT`.

Insufficiency: `NO_EVIDENCE`, `INSUFFICIENT_SUPPORTING_BLOCKS`, `INSUFFICIENT_INDEPENDENT_SOURCES`,
`ASSESSMENT_ONLY_EVIDENCE`, `TABLE_STRUCTURE_INCOMPLETE`, `FORMULA_SOURCE_MISSING`,
`VISUAL_INTERPRETATION_UNAVAILABLE`, `EVIDENCE_BUDGET_OMISSION`, `CONTEXT_INCOMPLETE`,
`RETRIEVAL_WARNING_PRESENT`.

Conflict: `ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE`, `INDEPENDENT_SOURCE_VALUE_CONFLICT`.

## Question kinds

Deterministic: a fixed versioned cue table (`question-kind-v1`) over the user's own analyzer terms,
falling back to the retrieved anchors' chunk types. No model, no rewriting, no paraphrase.
Classification only raises what the evidence must contain, so a misclassification abstains rather
than answering from less. Tokenization uses a locally declared analyzer config, not the tenant's
active index policy: rebuilding a lexical index must not change what the gate decides.

`ORDINARY_FACTUAL` needs one supporting block from one non-assessment source. `TABLE_DEPENDENT`
additionally needs every table part to carry its header rows. `FORMULA_DEPENDENT` needs a formula
artifact. `FIGURE_DEPENDENT` needs an approved vision-analysis path, which M7 does not have, so it
always abstains. `ASSESSMENT` needs a non-assessment source, because a question bank records what
an examiner marked rather than what the corpus establishes.

## Authority and conflict

Assessment source types (`QUESTION_BANK`, `QUESTION_PAPER`, `ANSWER_KEY`) and `ASSESSMENT`
authority never satisfy a requirement for a medical source on their own; ingestion already forbids
those source types from claiming `REFERENCE` or `HIGH`. A high CrossEncoder rank does not override
this.

Two deterministic detectors, both narrow and both over-triggering by design: an assessment key
whose content the reference evidence in the same set does not support, and two independent
non-assessment document versions stating different values for the same labelled quantity. One
document stating several values for one label is a range, not a disagreement. Different labels
sharing a unit do not conflict.

M7 detects disagreement already present in the evidence. It does not analyse claims in a generated
draft — that is M8, and the two must not merge.

## Policy

`SufficiencyConfig` is frozen and fingerprinted; the fingerprint appears in every decision and in
every draft, so a decision is reconstructable without persisting the question. Requirements are
per-kind `EvidenceRequirement` models. Nothing is calibrated: the defaults are conservative
starting points measured against a fixture written alongside them.
