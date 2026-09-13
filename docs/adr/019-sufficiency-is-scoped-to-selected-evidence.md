# ADR-019: Sufficiency is about the evidence that was selected

Status: accepted. Post-M12 remediation; no milestone was created. Supersedes the completeness
portion of ADR-012; every other decision in ADR-012 stands, including that no retrieval, fusion or
reranker score may reach the gate.

## Context

ADR-012 built the Evidence Sufficiency Gate to read structure rather than scores, and listed among
its signals "whether anything was omitted for budget, whether any block is a partial fragment".
Implemented, those two became global: `_completeness_checks` reasoned over the whole `EvidenceSet`
and every warning it carried, while every other check in the same class — counts, authority,
artifacts — was already scoped to the reranked anchors.

On the synthetic fixture corpus nothing exposed the difference. A real 932-page Medical
Microbiology textbook did, immediately. Across the 26-question acceptance set:

| Measurement | Result |
|---|---|
| Questions answered | **0 of 26** |
| Supported / context-dependent questions answered | **0 of 15**, defining passage at rank 1 |
| Anchors the reranker selected | 130 |
| Anchors admitted into the EvidenceSet | **130 — every one, every time** |
| Budget omissions concerning a selected anchor | **0** |
| Budget omissions concerning an unused optional candidate | **135** |
| Anchor blocks that were partial fragments | **0** |
| Other warnings (all `CONTEXT_INVALID_NEIGHBOUR`, expansion tier) | 19 |

Not one abstention was caused by a deficiency in the evidence actually selected. The gate was
answering "did anything anywhere go less than perfectly?" rather than "is the evidence I am about
to hand the generator complete?".

The repository had already recorded the symptom without naming it: **every** M7, M8 and M9
integration test constructed the gate with `budget_omission_is_insufficient=False` and
`incomplete_context_is_insufficient=False`. No integration test had ever reached a `SUFFICIENT`
outcome under the shipped defaults.

## Decision 1 — a warning blocks only when it touches selected evidence

Sufficiency is scoped to the final selected EvidenceSet and its required dependencies. A candidate
that was considered and not used cannot make the evidence that *was* used insufficient.

Three source lines produce every warning, and only that frame knows which tier it came from:

| Site | Meaning | Tier |
|---|---|---|
| `assembly.py` `add()` | a candidate did not fit the budget | `ANCHOR` or `EXPANSION`, depending on the call |
| `assembly.py` sibling check | a sibling was not reading-order contiguous, so it was skipped | always `EXPANSION` |
| `assembly.py` parent mismatch | a relative belongs to a different parent | **raises**; aborts assembly, never reaches the gate |

The last two share a name and have nothing else in common — which is why the classification is
established from the emitting site, not from the code string.

## Decision 2 — warnings are structured, and unknown means blocking

`EvidenceWarning` carries `code`, `chunk_id`, `tier`, `selected_by_reranker` and
`required_dependency`. `EvidenceSet.warnings` keeps its string form unchanged for the inspector and
existing readers; `warning_details` is additive.

`blocking()` is an exhaustive table with a default-deny tail:

- a `required_dependency` warning blocks, whatever its code;
- integrity codes always block — `SPARSE_LANE_UNAVAILABLE_DEGRADED_TO_DENSE` and
  `CANDIDATE_WITHOUT_PROVENANCE_DROPPED` are exactly the "corpus/index integrity is uncertain" and
  "provenance is invalid" cases the safety contract requires an abstention for;
- `CONTEXT_BUDGET_EXCEEDED` and `CONTEXT_INVALID_NEIGHBOUR` are advisory **only** on positive proof
  that they name an expansion-tier candidate the reranker did not select. An untiered warning — a
  legacy string, or one from a caller that did not say — is not proof, and blocks;
- anything else blocks.

Adding a warning upstream can therefore never quietly widen what the gate allows.

## Decision 3 — a partial anchor is not automatically incomplete

`representation` is `source-spans-v1` for **every expansion block by construction**, so the old
rule meant that enabling context expansion at all guaranteed insufficiency. It is also assigned to
an anchor whose spans were trimmed — and `remaining()` trims exactly the regions an
already-admitted block covers. Trimming removes duplication; it does not truncate.

Blocking now requires a *materially* partial anchor: `expansion_reason == RERANKED_ANCHOR`,
`representation == source-spans-v1`, and `trimmed_text_present_elsewhere == False`. Assembly
computes that flag by checking each trimmed region against the intervals already admitted, rather
than assuming the invariant holds, so a future change to deduplication cannot silently present a
truncated anchor as complete.

## Decision 4 — the required-parent dependency

The expander requests a parent **only** when the anchor does not end in sentence-final
punctuation — that is, only when the anchor reads as a mid-sentence fragment. A parent in that
situation is not optional extra context; it is what makes the selected anchor interpretable.

An admitted anchor that is a `TEXT_CHILD`/`TEXT` with a parent and no sentence-final ending is
recorded as needing one. If no parent is admitted — whether it was dropped for budget or never
offered as a relative — assembly emits `CONTEXT_REQUIRED_PARENT_MISSING` with
`required_dependency=True`, and the gate abstains with `REQUIRED_CONTEXT_MISSING`.

This is the whole of the dependency model. No general dependency graph is built: the tier
distinction plus this one conditional requirement covers every case the architecture produces.

## Consequences

**Some unsupported-question abstention moves from M7 to M8, deliberately.** The gate has no
relevance signal and never had one — ADR-012 forbids any score from reaching it, and no check
compares the question to the evidence. The apparent safety of the old behaviour on off-topic
questions was *accidental*: a budget rule firing on unrelated candidates, not a judgement that the
evidence was off-topic. Rescoping does not remove a designed protection; it reveals that the
designed protection for "the corpus does not answer this" is M8 claim verification.

Accepted costs: an off-topic question may now consume one provider call before abstaining, and its
outcome may be an unverified abstention rather than `INSUFFICIENT_EVIDENCE`. The acceptance
criterion is that unsupported questions still end in a **non-answer**, not that they end in a
particular code. `AskResponse` still makes an answer unrepresentable alongside any non-verified
outcome.

Unchanged: evidence budgets, claim verification, retrieval and reranking, every `EvidenceRequirement`
threshold, and the `SufficiencyConfig` flags themselves — `budget_omission_is_insufficient` and
`incomplete_context_is_insufficient` keep their shipped `True`. What changed is *what they are
computed over*. The M7/M8/M9 integration suites now pass with those defaults in place, which is the
check that the semantics are usable rather than merely looser.

Not addressed here: the known "left shift" retrieval miss. If evidence is not retrieved,
sufficiency still fails, and it should. BM25/RRF ranking quality is a separate question.
