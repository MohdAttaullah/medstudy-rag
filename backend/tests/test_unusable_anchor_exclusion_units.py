"""An anchor whose required context cannot be satisfied is removed, not flagged.

Leaving it in `evidence_blocks` with a warning was not safe. Generation renders that list and
derives the citable ids from it, and verification reads the same list, so the only thing keeping an
incomplete fragment out of the prompt was the whole-question abstention -- which also discarded the
complete anchors that independently answered the question.

Removal is what makes the safety properties structural rather than conditional. These tests hold
that line: excluded from the blocks, from the anchors, from the counts, from the prompt and from
the citable set, while conflict detection still sees everything that was assembled and a required
dependency on a *kept* anchor still blocks. See ADR-020.
"""

from uuid import UUID, uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, GroundingConfig, SufficiencyConfig
from app.evidence.model import EvidenceBlock, EvidenceSet, EvidenceWarning
from app.generation.citations.validate import bind
from app.generation.errors import GenerationError
from app.generation.grounding.model import AnswerDraft, DraftClaim
from app.generation.prompts.grounded import render_evidence
from app.sufficiency.conflicts import detect
from app.sufficiency.gate import SufficiencyGate

QUESTION = "How is warfarin metabolised?"
REMOVED_MARKER = "UNUSABLE FRAGMENT text that must never be shown"


def gate(**requirement) -> SufficiencyGate:
    return SufficiencyGate(SufficiencyConfig(ordinary=EvidenceRequirement(**requirement)))


def block(
    anchor: UUID | None = None,
    *,
    reason: str = "RERANKED_ANCHOR",
    text: str = "Warfarin is metabolised by CYP2C9.",
    source_type: str = "TEXTBOOK",
    authority: str = "REFERENCE",
    chunk_type: str = "TEXT_CHILD",
    artifacts: list | None = None,
    question: dict | None = None,
) -> EvidenceBlock:
    owner = anchor or uuid4()
    return EvidenceBlock(
        evidence_id=uuid4(),
        anchor_chunk_id=owner,
        source_chunk_ids=[owner],
        source_element_ids=[uuid4()],
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_run_id=uuid4(),
        parse_run_id=uuid4(),
        document_title="Medical Microbiology",
        source_type=source_type,
        authority_level=authority,
        chunk_type=chunk_type,
        pages=[13],
        hierarchy=[],
        source_spans=[],
        text=text,
        representation="m3-source-with-structural-labels-v1",
        trimmed_text_present_elsewhere=True,
        artifacts=artifacts or [],
        question=question,
        expansion_reason=reason,
        context_reasons=[reason],
        token_count=12,
        requires_visual_evidence=False,
    )


def unusable_warning(anchor: UUID) -> EvidenceWarning:
    """Exactly what assembly emits when a required completion could not be admitted."""
    return EvidenceWarning(
        code="CONTEXT_REQUIRED_PARENT_MISSING",
        chunk_id=anchor,
        tier="ANCHOR",
        selected_by_reranker=True,
        required_dependency=True,
        anchor_chunk_id=anchor,
    )


def evidence(
    usable: list[EvidenceBlock],
    *,
    excluded_blocks: list[EvidenceBlock] | None = None,
    excluded_anchors: list[UUID] | None = None,
    warnings: list[EvidenceWarning] | None = None,
) -> EvidenceSet:
    details = list(warnings or [])
    return EvidenceSet(
        query_hash="h",
        retrieval_trace={},
        reranking_trace={},
        anchors=[b.anchor_chunk_id for b in usable if b.expansion_reason == "RERANKED_ANCHOR"],
        expansions=[b.evidence_id for b in usable if b.expansion_reason != "RERANKED_ANCHOR"],
        evidence_blocks=usable,
        total_tokens=sum(b.token_count for b in usable),
        requires_visual_evidence=any(b.requires_visual_evidence for b in usable),
        warnings=[w.render() for w in details],
        warning_details=details,
        excluded_anchors=list(excluded_anchors or []),
        excluded_blocks=list(excluded_blocks or []),
        duplicates_removed=0,
    )


def one_excluded() -> tuple[EvidenceSet, UUID, list[EvidenceBlock]]:
    """Four complete anchors and one unusable, split the way the service splits them."""
    bad = uuid4()
    complete = [block() for _ in range(4)]
    removed = [
        block(bad, text=REMOVED_MARKER),
        block(bad, reason="PARENT_EXPANSION", text="Context for the removed anchor."),
        block(bad, reason="NEXT_SIBLING", text="More context for the removed anchor."),
    ]
    return (
        evidence(
            complete,
            excluded_blocks=removed,
            excluded_anchors=[bad],
            warnings=[unusable_warning(bad)],
        ),
        bad,
        complete,
    )


# --------------------------------------------------------------- the core behaviour


def test_the_remaining_anchors_are_evaluated_normally_and_can_succeed():
    """One unusable anchor no longer invalidates four that independently answer the question."""
    found, _, complete = one_excluded()
    decision = gate().evaluate(QUESTION, found)
    assert decision.status == "SUFFICIENT"
    assert "REQUIRED_CONTEXT_MISSING" not in decision.reason_codes
    assert len(found.evidence_blocks) == len(complete)


def test_the_excluded_anchor_is_absent_from_evidence_blocks():
    found, bad, _ = one_excluded()
    assert all(b.anchor_chunk_id != bad for b in found.evidence_blocks)
    assert REMOVED_MARKER not in " ".join(b.text for b in found.evidence_blocks)


def test_the_excluded_anchor_is_absent_from_the_anchor_list():
    found, bad, _ = one_excluded()
    assert bad not in found.anchors
    assert found.excluded_anchors == [bad]


def test_every_expansion_owned_by_the_excluded_anchor_is_removed():
    """Context for evidence that no longer exists is not context."""
    found, bad, _ = one_excluded()
    removed_ids = {r.evidence_id for r in found.excluded_blocks}
    assert not [b for b in found.evidence_blocks if b.anchor_chunk_id == bad]
    assert not [e for e in found.expansions if e in removed_ids]
    assert {b.expansion_reason for b in found.excluded_blocks} == {
        "RERANKED_ANCHOR",
        "PARENT_EXPANSION",
        "NEXT_SIBLING",
    }


def test_the_exclusion_stays_visible_in_the_decision():
    """Removed is not hidden: the warning survives and the decision reports the exclusion."""
    found, _, _ = one_excluded()
    decision = gate().evaluate(QUESTION, found)
    assert any(w.startswith("CONTEXT_REQUIRED_PARENT_MISSING") for w in found.warnings)
    assert "ADVISORY_CONTEXT_OMISSION" in decision.reason_codes
    signal = next(s for s in decision.evaluated_signals if s.name == "anchors_excluded_as_unusable")
    assert signal.value == 1
    advisory = next(s for s in decision.evaluated_signals if s.name == "advisory_warnings")
    assert "CONTEXT_REQUIRED_PARENT_MISSING" in advisory.value


def test_the_excluded_anchor_does_not_count_toward_the_block_minimum():
    """Four complete anchors plus one excluded must not satisfy a minimum of five."""
    found, _, _ = one_excluded()
    decision = gate(min_supporting_blocks=5).evaluate(QUESTION, found)
    assert decision.status == "INSUFFICIENT"
    assert "INSUFFICIENT_SUPPORTING_BLOCKS" in decision.reason_codes
    counted = next(s for s in decision.evaluated_signals if s.name == "supporting_blocks")
    assert counted.value == 4


# ------------------------------------------------------ what the later layers read


def test_the_excluded_anchor_never_reaches_the_generation_prompt():
    """M7 renders `evidence_blocks`; nothing else is offered to the provider."""
    found, _, _ = one_excluded()
    rendered = render_evidence(found.evidence_blocks, GroundingConfig())
    assert REMOVED_MARKER not in rendered
    for removed in found.excluded_blocks:
        assert str(removed.evidence_id) not in rendered
        assert removed.text not in rendered


def test_the_excluded_anchor_never_enters_the_citable_set():
    """M7 derives `approved` from the same list, and binding refuses anything outside it."""
    found, _, _ = one_excluded()
    grounding = GroundingConfig()
    approved = [b.evidence_id for b in found.evidence_blocks[: grounding.max_evidence_blocks]]
    removed_id = found.excluded_blocks[0].evidence_id
    assert removed_id not in approved

    draft = AnswerDraft(
        answer="Warfarin is metabolised by CYP2C9.",
        claims=[DraftClaim(text="Warfarin is metabolised by CYP2C9.", evidence_ids=[removed_id])],
    )
    with pytest.raises(GenerationError, match="GENERATION_UNKNOWN_CITATION"):
        bind(draft, approved)


def test_the_excluded_anchor_never_reaches_verification_evidence():
    """M8 verifies claims against `evidence_blocks`, so an excluded block supports nothing."""
    found, _, _ = one_excluded()
    supplied = {b.evidence_id: b for b in found.evidence_blocks}
    for removed in found.excluded_blocks:
        assert removed.evidence_id not in supplied


# --------------------------------------------------------------- abstention paths


def test_all_anchors_unusable_yields_no_evidence():
    bad = [uuid4(), uuid4()]
    found = evidence(
        [],
        excluded_blocks=[block(bad[0]), block(bad[1])],
        excluded_anchors=bad,
        warnings=[unusable_warning(i) for i in bad],
    )
    decision = gate().evaluate(QUESTION, found)
    assert decision.status == "INSUFFICIENT"
    assert "NO_EVIDENCE" in decision.reason_codes
    assert decision.supporting_evidence_ids == []


def test_remaining_anchors_failing_the_count_requirement_abstain():
    bad = uuid4()
    found = evidence(
        [block()],
        excluded_blocks=[block(bad)],
        excluded_anchors=[bad],
        warnings=[unusable_warning(bad)],
    )
    decision = gate(min_supporting_blocks=3).evaluate(QUESTION, found)
    assert decision.status == "INSUFFICIENT"
    assert "INSUFFICIENT_SUPPORTING_BLOCKS" in decision.reason_codes


def test_remaining_anchors_failing_the_authority_requirement_abstain():
    """The exclusion leaves only assessment material, so no medical source remains."""
    bad = uuid4()
    found = evidence(
        [block(source_type="QUESTION_BANK", authority="ASSESSMENT")],
        excluded_blocks=[block(bad)],
        excluded_anchors=[bad],
        warnings=[unusable_warning(bad)],
    )
    decision = gate().evaluate(QUESTION, found)
    assert decision.status == "INSUFFICIENT"
    assert "ASSESSMENT_ONLY_EVIDENCE" in decision.reason_codes


def test_remaining_anchors_failing_the_artifact_requirement_abstain():
    """A table question whose surviving evidence carries no table structure still abstains."""
    bad = uuid4()
    found = evidence(
        [block(chunk_type="TABLE", text="Rows without any declared structure.")],
        excluded_blocks=[block(bad)],
        excluded_anchors=[bad],
        warnings=[unusable_warning(bad)],
    )
    decision = gate().evaluate("Which row of the table lists the dose?", found)
    assert decision.status == "INSUFFICIENT"
    assert "TABLE_STRUCTURE_INCOMPLETE" in decision.reason_codes


# ----------------------------------------------------------------------- conflict


def test_a_conflict_involving_an_excluded_anchor_is_still_detected():
    """Filtering must not be a way to make a disagreement disappear."""
    bad = uuid4()
    keeper = block(text="The reported mortality is 15%.")
    removed = block(bad, text="The reported mortality is 75%.")
    found = evidence(
        [keeper],
        excluded_blocks=[removed],
        excluded_anchors=[bad],
        warnings=[unusable_warning(bad)],
    )

    # The keeper alone disagrees with nothing. The pair does, and the gate must see the pair.
    assert not detect([keeper])
    assert detect([keeper, removed])

    decision = gate().evaluate("What is the reported mortality?", found)
    assert decision.status == "CONFLICTING"
    assert "INDEPENDENT_SOURCE_VALUE_CONFLICT" in decision.reason_codes
    assert removed.evidence_id in decision.conflicting_evidence_ids


def test_an_unsupported_assessment_key_survives_the_exclusion():
    """The other detector, on the same rule: conflict is judged over everything assembled."""
    bad = uuid4()
    keeper = block(text="Legionella grows on buffered charcoal yeast extract agar.")
    removed = block(
        bad,
        source_type="QUESTION_BANK",
        authority="ASSESSMENT",
        question={"explicit_answer": "Thayer-Martin selective medium"},
    )
    found = evidence(
        [keeper],
        excluded_blocks=[removed],
        excluded_anchors=[bad],
        warnings=[unusable_warning(bad)],
    )
    decision = gate().evaluate("Which medium grows Legionella?", found)
    assert decision.status == "CONFLICTING"
    assert "ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE" in decision.reason_codes


# -------------------------------------------------------------------- fail closed


def test_a_required_dependency_on_a_kept_anchor_still_blocks():
    """The gate must not be the thing that lets an unfiltered incomplete anchor through."""
    kept = block()
    found = evidence([kept], warnings=[unusable_warning(kept.anchor_chunk_id)])
    decision = gate().evaluate(QUESTION, found)
    assert decision.status == "INSUFFICIENT"
    assert "REQUIRED_CONTEXT_MISSING" in decision.reason_codes


def test_a_required_dependency_on_a_kept_anchor_blocks_even_alongside_an_exclusion():
    """Partial filtering is the dangerous case: one anchor removed, another still incomplete."""
    found, _, complete = one_excluded()
    still_broken = complete[0].anchor_chunk_id
    with_second = found.model_copy(
        update={"warning_details": [*found.warning_details, unusable_warning(still_broken)]}
    )
    decision = gate().evaluate(QUESTION, with_second)
    assert decision.status == "INSUFFICIENT"
    assert "REQUIRED_CONTEXT_MISSING" in decision.reason_codes


def test_an_unknown_warning_still_blocks_after_an_exclusion():
    found, _, _ = one_excluded()
    with_unknown = found.model_copy(
        update={
            "warning_details": [
                *found.warning_details,
                EvidenceWarning(code="SOMETHING_NEW", tier="EXPANSION"),
            ]
        }
    )
    decision = gate().evaluate(QUESTION, with_unknown)
    assert decision.status == "INSUFFICIENT"
    assert "RETRIEVAL_WARNING_PRESENT" in decision.reason_codes


def test_an_integrity_warning_still_blocks_after_an_exclusion():
    found, _, _ = one_excluded()
    with_integrity = found.model_copy(
        update={
            "warning_details": [
                *found.warning_details,
                EvidenceWarning(code="CANDIDATE_WITHOUT_PROVENANCE_DROPPED"),
            ]
        }
    )
    decision = gate().evaluate(QUESTION, with_integrity)
    assert decision.status == "INSUFFICIENT"
    assert "RETRIEVAL_WARNING_PRESENT" in decision.reason_codes


# --------------------------------------------------------------------- determinism


def test_the_same_input_gives_the_same_decision_and_order():
    found, _, _ = one_excluded()
    first = gate().evaluate(QUESTION, found)
    second = gate().evaluate(QUESTION, found)
    assert first.status == second.status
    assert first.reason_codes == second.reason_codes
    assert first.supporting_evidence_ids == second.supporting_evidence_ids
    assert [s.name for s in first.evaluated_signals] == [s.name for s in second.evaluated_signals]


@pytest.mark.parametrize("count", [1, 2, 3])
def test_any_number_of_exclusions_leaves_the_rest_judged_normally(count):
    bad = [uuid4() for _ in range(count)]
    found = evidence(
        [block() for _ in range(2)],
        excluded_blocks=[block(i) for i in bad],
        excluded_anchors=bad,
        warnings=[unusable_warning(i) for i in bad],
    )
    decision = gate().evaluate(QUESTION, found)
    assert decision.status == "SUFFICIENT"
    signal = next(s for s in decision.evaluated_signals if s.name == "anchors_excluded_as_unusable")
    assert signal.value == count
