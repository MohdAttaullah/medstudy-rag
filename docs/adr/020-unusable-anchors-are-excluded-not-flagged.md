# ADR-020: An unusable anchor is excluded from the EvidenceSet, not flagged inside it

Status: accepted. Post-M12 remediation; no milestone was created. Extends ADR-019, which scoped
sufficiency to the selected evidence. Nothing in ADR-012 or ADR-019 is reversed.

## Context

ADR-019 stopped the gate from failing a question because an *unselected* candidate had a problem.
It left a second, narrower coarseness in place: a problem with **one** selected anchor still
invalidated the whole question.

The mechanism was `CONTEXT_REQUIRED_PARENT_MISSING`. An anchor that ends mid-sentence needs the
text that completes it; when that completion cannot be admitted, assembly records a warning with
`required_dependency=True`, and `blocking()` treats a required dependency as blocking regardless of
tier — correctly, because an incomplete fragment is not evidence. But the warning was a property of
the *set*, so the gate's only available verdict was to refuse the entire question.

Measured on the 932-page Medical Microbiology textbook over the 26-question acceptance set, after
ADR-019 and the bounded-completion work:

| Measurement | Result |
|---|---|
| Supported / context-dependent questions | 15 |
| Answered (VERIFIED) | 3 |
| Abstained with at least one *complete* anchor present | 11 |
| Questions where exactly one selected anchor was unusable | 7 |
| Questions blocked *solely* by `REQUIRED_CONTEXT_MISSING` | 4 |

In the four solely-blocked cases the EvidenceSet contained complete, independently sufficient
anchors — in several, the passage that answers the question at rank one — and the question was
refused because a different anchor, ranked lower, was a fragment. That is not fail-closed
behaviour on the evidence being used; it is an unrelated defect voting against evidence that is
fine.

The safety property that mattered was never the abstention itself. It was that the fragment must
not reach the generator. Three consumers read `EvidenceSet.evidence_blocks` and nothing else:

- `services/generation.py` renders it into the prompt and derives `approved` (the citable ids)
  from it;
- `generation/citations/validate.bind` refuses any citation outside `approved`;
- `services/verification.py` builds the claim-verification evidence from the same list.

So while the fragment sat in `evidence_blocks`, the *only* thing keeping it out of the prompt and
out of the citable set was the whole-question abstention. The safety property was conditional on
the refusal, and the refusal was costing eleven answerable questions.

## Decision — remove the anchor, do not annotate it

An anchor whose required context could not be satisfied is **unusable evidence**, and unusable
evidence is removed from the set the rest of the pipeline consumes.

Concretely, in `services/evidence.py`, at the point the EvidenceSet is constructed:

1. `unusable` = the anchors named by `CONTEXT_REQUIRED_PARENT_MISSING` warnings.
2. Those anchors' blocks — the anchor block **and every expansion belonging to it** — are removed
   from `evidence_blocks`, `anchors` and `expansions`. Context for evidence that no longer exists
   is not context.
3. `total_tokens` and `requires_visual_evidence` are recomputed over what survives.
4. The removed anchors are recorded in `EvidenceSet.excluded_anchors`, and their blocks in
   `EvidenceSet.excluded_blocks`.

`EvidenceWarning` gained `anchor_chunk_id`, because a dropped-parent warning names the *parent* in
`chunk_id`; without it the anchor the parent was meant to complete could not be identified. All
three assembly warning sites now populate it.

The exclusion is not concealment. The warning stays in `warnings` and `warning_details`, the
decision carries a new `anchors_excluded_as_unusable` signal, and `ADVISORY_CONTEXT_OMISSION`
appears in the reason codes.

### Why this is safer than the flag, not merely more permissive

Removal converts four conditional safety properties into structural ones. An excluded anchor is
absent from the list generation renders, absent from `approved` so `bind` raises
`GENERATION_UNKNOWN_CITATION` on any attempt to cite it, absent from the verification evidence, and
absent from the count the gate compares against `min_supporting_blocks`. None of that now depends
on the gate reaching any particular verdict.

### What is deliberately unchanged

- **Conflict detection runs over the full assembled set**, excluded blocks included:
  `conflict_detection.detect(blocks + list(evidence.excluded_blocks))`. A disagreement must not
  disappear because one of its participants was filtered out for being incomplete. A conflict
  involving an excluded anchor still yields `CONFLICTING`.
- **A required dependency on a *kept* anchor still blocks.** `_was_excluded` only makes a warning
  advisory when it concerns an anchor actually in `excluded_anchors`; anything else keeps its
  existing classification. If the filter ever failed to remove a block, the gate is still the
  thing that refuses.
- **Default-deny on unknown warnings is untouched.** An unknown or untiered warning blocks.
- Thresholds, budgets, `min_supporting_blocks`, retrieval, reranking, chunking, generation policy
  and verification policy are all unchanged.

### Consequences

Questions are now judged on the evidence that survives. A question whose only anchors are unusable
reaches the gate with an empty block list and abstains with `NO_EVIDENCE` — the same refusal as
before, reached through absence of evidence rather than presence of a warning. A question whose
survivors fail the count, authority or artifact requirement abstains for that reason, named
precisely.

The cost is that an EvidenceSet can now be answered from fewer anchors than the reranker selected,
without that reduction itself being a refusal. That is the intended change: the number of anchors
selected was never a sufficiency criterion, and `min_supporting_blocks` remains the criterion that
is.

## Alternatives considered

**A. Keep the block, weaken the warning to advisory.** Rejected: it leaves the fragment in
`evidence_blocks`, so it is rendered into the prompt and is citable. That is strictly less safe
than today's behaviour, not more.

**B. Exclude the anchor (chosen).**

**C. Admit the fragment with a truncation marker.** Rejected: it invents a boundary the source does
not have, and an anchor whose sentence never completes is exactly the material the grounding policy
exists to keep out of a medical answer.
