"""Sufficiency is about the evidence that was selected, not every candidate considered.

Before ADR-019 the gate refused whenever *anything* encountered during assembly produced a
warning. On a real 932-page textbook that made 15 of 15 answerable questions unanswerable while
their defining passages sat at rank one: 135 budget omissions across 26 questions, every one of
them an optional expansion nobody selected, and not a single dropped anchor.

These tests fix the distinction. Advisory means proven to be unused optional context; everything
else — including anything unrecognised — blocks.
"""

from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.evidence.model import EvidenceBlock, EvidenceSet, EvidenceWarning
from app.sufficiency.gate import SufficiencyGate, blocking

QUESTION = "How is warfarin metabolised?"


def gate(**overrides) -> SufficiencyGate:
    return SufficiencyGate(SufficiencyConfig(ordinary=EvidenceRequirement(), **overrides))


def block(
    reason: str = "RERANKED_ANCHOR",
    representation: str = "m3-source-with-structural-labels-v1",
    covered: bool = True,
) -> EvidenceBlock:
    anchor = uuid4()
    return EvidenceBlock(
        evidence_id=uuid4(),
        anchor_chunk_id=anchor,
        source_chunk_ids=[anchor],
        source_element_ids=[uuid4()],
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_run_id=uuid4(),
        parse_run_id=uuid4(),
        document_title="Medical Microbiology",
        source_type="TEXTBOOK",
        authority_level="REFERENCE",
        chunk_type="TEXT_CHILD",
        pages=[13],
        hierarchy=[],
        source_spans=[],
        text="Warfarin is metabolised by CYP2C9.",
        representation=representation,
        trimmed_text_present_elsewhere=covered,
        artifacts=[],
        question=None,
        expansion_reason=reason,
        context_reasons=[reason],
        token_count=12,
        requires_visual_evidence=False,
    )


def evidence(*blocks: EvidenceBlock, warnings: list[EvidenceWarning] | None = None) -> EvidenceSet:
    details = list(warnings or [])
    return EvidenceSet(
        query_hash="h",
        retrieval_trace={},
        reranking_trace={},
        anchors=[b.anchor_chunk_id for b in blocks if b.expansion_reason == "RERANKED_ANCHOR"],
        expansions=[],
        evidence_blocks=list(blocks),
        total_tokens=sum(b.token_count for b in blocks),
        requires_visual_evidence=False,
        warnings=[w.render() for w in details],
        warning_details=details,
        duplicates_removed=0,
    )


def omitted(tier: str, selected: bool, required: bool = False) -> EvidenceWarning:
    return EvidenceWarning(
        code="CONTEXT_BUDGET_EXCEEDED",
        chunk_id=uuid4(),
        tier=tier,
        selected_by_reranker=selected,
        required_dependency=required,
    )


# ------------------------------------------------------------------ budget omission scope


def test_an_omitted_selected_anchor_blocks():
    """A reranked anchor that did not fit is evidence the answer was meant to rest on."""
    decision = gate().evaluate(QUESTION, evidence(block(), warnings=[omitted("ANCHOR", True)]))
    assert decision.status == "INSUFFICIENT"
    assert "EVIDENCE_BUDGET_OMISSION" in decision.reason_codes


def test_an_omitted_optional_expansion_is_advisory():
    """The real-book case: 135 of 135 omissions were optional context nobody selected."""
    decision = gate().evaluate(QUESTION, evidence(block(), warnings=[omitted("EXPANSION", False)]))
    assert decision.status == "SUFFICIENT"
    assert "EVIDENCE_BUDGET_OMISSION" not in decision.reason_codes


def test_an_advisory_omission_stays_visible_in_the_decision():
    """Not blocking is not the same as not reported."""
    decision = gate().evaluate(QUESTION, evidence(block(), warnings=[omitted("EXPANSION", False)]))
    assert "ADVISORY_CONTEXT_OMISSION" in decision.reason_codes
    names = {s.name for s in decision.evaluated_signals}
    assert {"budget_omissions_unused_candidates", "advisory_warnings"} <= names


def test_a_mixture_blocks_on_the_selected_one():
    decision = gate().evaluate(
        QUESTION,
        evidence(block(), warnings=[omitted("EXPANSION", False), omitted("ANCHOR", True)]),
    )
    assert decision.status == "INSUFFICIENT"
    assert "EVIDENCE_BUDGET_OMISSION" in decision.reason_codes


# --------------------------------------------------------------------- partial anchors


def test_a_materially_partial_anchor_blocks():
    """Trimmed text that is nowhere else in the set is missing source."""
    decision = gate().evaluate(
        QUESTION, evidence(block(representation="source-spans-v1", covered=False))
    )
    assert decision.status == "INSUFFICIENT"
    assert "CONTEXT_INCOMPLETE" in decision.reason_codes


def test_a_deduplicated_partial_anchor_does_not_block():
    """Trimming removes duplication; the text is carried by another block in the same set."""
    decision = gate().evaluate(
        QUESTION, evidence(block(representation="source-spans-v1", covered=True))
    )
    assert decision.status == "SUFFICIENT"
    assert "CONTEXT_INCOMPLETE" not in decision.reason_codes


def test_a_source_spans_expansion_never_blocks_on_its_representation():
    """Every expansion is source-spans-v1 by construction, so this rule refused whole corpora."""
    decision = gate().evaluate(
        QUESTION,
        evidence(block(), block(reason="NEXT_SIBLING", representation="source-spans-v1")),
    )
    assert decision.status == "SUFFICIENT"
    assert "CONTEXT_INCOMPLETE" not in decision.reason_codes


# ------------------------------------------------------------- required parent dependency


def test_a_missing_required_parent_blocks():
    """An anchor that reads as a mid-sentence fragment is not complete without its parent."""
    missing = EvidenceWarning(
        code="CONTEXT_REQUIRED_PARENT_MISSING",
        chunk_id=uuid4(),
        tier="ANCHOR",
        selected_by_reranker=True,
        required_dependency=True,
    )
    decision = gate().evaluate(QUESTION, evidence(block(), warnings=[missing]))
    assert decision.status == "INSUFFICIENT"
    assert "REQUIRED_CONTEXT_MISSING" in decision.reason_codes
    assert "required_context" in decision.missing_requirements


def test_a_required_dependency_blocks_whatever_its_code():
    """Required is required: the flag decides, not the code."""
    decision = gate().evaluate(
        QUESTION, evidence(block(), warnings=[omitted("EXPANSION", False, required=True)])
    )
    assert decision.status == "INSUFFICIENT"
    assert "REQUIRED_CONTEXT_MISSING" in decision.reason_codes


def test_a_present_required_parent_leaves_the_set_eligible():
    """The parent was admitted, so no warning exists and nothing blocks."""
    decision = gate().evaluate(
        QUESTION,
        evidence(block(), block(reason="PARENT_EXPANSION", representation="source-spans-v1")),
    )
    assert decision.status == "SUFFICIENT"


# ------------------------------------------------------------------ neighbour warnings


def test_the_invalid_neighbour_warning_is_advisory():
    """It names a sibling that was never admitted; it says nothing about selected evidence."""
    warning = EvidenceWarning(code="CONTEXT_INVALID_NEIGHBOUR", chunk_id=uuid4(), tier="EXPANSION")
    decision = gate().evaluate(QUESTION, evidence(block(), warnings=[warning]))
    assert decision.status == "SUFFICIENT"
    assert "RETRIEVAL_WARNING_PRESENT" not in decision.reason_codes
    assert "ADVISORY_CONTEXT_OMISSION" in decision.reason_codes


def test_the_invalid_neighbour_hard_error_path_is_untouched():
    """The parent-mismatch path raises during assembly and never reaches the gate at all."""
    from pathlib import Path

    source = Path("backend/app/evidence/assembly.py").read_text(encoding="utf-8")
    assert 'raise RerankingError("CONTEXT_INVALID_NEIGHBOUR")' in source
    assert 'raise RerankingError("CONTEXT_SOURCE_LINEAGE_MISMATCH")' in source


# ------------------------------------------------------------------------- default deny


def test_an_unknown_warning_code_blocks():
    """Adding a warning somewhere upstream must not be able to widen what the gate allows."""
    unknown = EvidenceWarning(code="SOMETHING_NOBODY_CLASSIFIED_YET", tier="EXPANSION")
    decision = gate().evaluate(QUESTION, evidence(block(), warnings=[unknown]))
    assert decision.status == "INSUFFICIENT"
    assert "RETRIEVAL_WARNING_PRESENT" in decision.reason_codes


@pytest.mark.parametrize(
    "code", ["SPARSE_LANE_UNAVAILABLE_DEGRADED_TO_DENSE", "CANDIDATE_WITHOUT_PROVENANCE_DROPPED"]
)
def test_integrity_warnings_always_block(code):
    """Degraded lanes and provenance-less candidates are the corpus-integrity abstention cases."""
    decision = gate().evaluate(QUESTION, evidence(block(), warnings=[EvidenceWarning(code=code)]))
    assert decision.status == "INSUFFICIENT"
    assert "RETRIEVAL_WARNING_PRESENT" in decision.reason_codes


def test_an_untiered_tier_sensitive_warning_blocks():
    """A legacy string carries no tier, and unknown tier is not proof of harmlessness."""
    legacy = EvidenceSet(
        query_hash="h",
        retrieval_trace={},
        reranking_trace={},
        anchors=[],
        expansions=[],
        evidence_blocks=[block()],
        total_tokens=12,
        requires_visual_evidence=False,
        warnings=["CONTEXT_BUDGET_EXCEEDED:" + str(uuid4())],
        duplicates_removed=0,
    )
    assert gate().evaluate(QUESTION, legacy).status == "INSUFFICIENT"


@pytest.mark.parametrize(
    "warning,expected",
    [
        (EvidenceWarning(code="CONTEXT_BUDGET_EXCEEDED", tier="EXPANSION"), False),
        (EvidenceWarning(code="CONTEXT_BUDGET_EXCEEDED", tier="ANCHOR"), True),
        (EvidenceWarning(code="CONTEXT_BUDGET_EXCEEDED", tier="RETRIEVAL"), True),
        (EvidenceWarning(code="CONTEXT_INVALID_NEIGHBOUR", tier="EXPANSION"), False),
        (EvidenceWarning(code="CONTEXT_REQUIRED_PARENT_MISSING", tier="ANCHOR"), True),
        (EvidenceWarning(code="UNSEEN_CODE", tier="EXPANSION"), True),
    ],
)
def test_the_classification_table_is_explicit(warning, expected):
    assert blocking(warning) is expected


# ------------------------------------------------------------- unchanged elsewhere


def test_evidence_with_nothing_wrong_is_still_sufficient():
    decision = gate().evaluate(QUESTION, evidence(block()))
    assert decision.status == "SUFFICIENT"
    assert "SUPPORTED_BY_SOURCE_EVIDENCE" in decision.reason_codes


def test_no_evidence_is_still_insufficient():
    decision = gate().evaluate(QUESTION, evidence())
    assert decision.status == "INSUFFICIENT"
    assert "NO_EVIDENCE" in decision.reason_codes


def test_requirement_checks_are_untouched_by_the_rescoping():
    """An advisory warning must not rescue evidence that fails a real requirement."""
    strict = SufficiencyConfig(ordinary=EvidenceRequirement(min_supporting_blocks=3))
    decision = SufficiencyGate(strict).evaluate(
        QUESTION, evidence(block(), warnings=[omitted("EXPANSION", False)])
    )
    assert decision.status == "INSUFFICIENT"
    assert "INSUFFICIENT_SUPPORTING_BLOCKS" in decision.reason_codes
