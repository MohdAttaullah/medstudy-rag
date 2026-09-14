"""A question is about a figure when it asks about one, not when one happens to rank.

The previous rule was `any anchor is FIGURE_CONTEXT`. On a real 932-page corpus that classified 11
of 26 questions FIGURE_DEPENDENT -- **none** of them from the question's own words -- and since no
vision path exists, every one was a permanent abstention with the answering passage sitting in the
textual anchors beside the caption that caused the refusal. ADR-012 said "a figure question"; the
implementation had widened that to "a question near a figure chunk".

What is deliberately kept: the question's own cues, the fail-closed floor for evidence that is
genuinely unreadable without pixels, and -- untouched, and the reason this is safe -- M8's
claim-level rule that any claim citing a figure block fails. See ADR-023.
"""

from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.evidence.model import ArtifactRef, EvidenceBlock, EvidenceSet
from app.sufficiency.gate import SufficiencyGate
from app.sufficiency.question import CLASSIFIER_VERSION, classify, readable, visual

ORDINARY = "What is passive immunization?"


def block(
    *,
    chunk_type: str = "TEXT_CHILD",
    text: str = "Passive immunization provides preformed antibody.",
    requires_visual_evidence: bool | None = None,
    artifacts: list[ArtifactRef] | None = None,
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
        chunk_type=chunk_type,
        pages=[13],
        hierarchy=[],
        source_spans=[],
        text=text,
        representation="m3-source-with-structural-labels-v1",
        trimmed_text_present_elsewhere=True,
        artifacts=artifacts or [],
        question=None,
        expansion_reason="RERANKED_ANCHOR",
        context_reasons=["RERANKED_ANCHOR"],
        token_count=12,
        requires_visual_evidence=(
            chunk_type == "FIGURE_CONTEXT"
            if requires_visual_evidence is None
            else requires_visual_evidence
        ),
    )


def figure(text: str = "FIGURE 11-1 Types of immunizations.") -> EvidenceBlock:
    return block(chunk_type="FIGURE_CONTEXT", text=text)


def silent_figure() -> EvidenceBlock:
    """A figure whose neighbourhood yielded no caption or prose. M3 flags this at ingestion."""
    return block(chunk_type="FIGURE_CONTEXT", text="")


# ------------------------------------------------------- the question decides visual dependence


@pytest.mark.parametrize(
    "question",
    [
        "What does Figure 12 show?",
        "Based on the image, identify the organism.",
        "What structure is indicated by the arrow?",
    ],
)
def test_a_question_that_asks_to_read_a_picture_is_figure_dependent(question):
    assert classify(question, []) == "FIGURE_DEPENDENT"


@pytest.mark.parametrize(
    "cue",
    [
        "figure",
        "figures",
        "image",
        "images",
        "diagram",
        "diagrams",
        "illustration",
        "illustrated",
        "graph",
        "chart",
        "depicted",
        "shown",
        "picture",
        "pictured",
        "photograph",
        "micrograph",
        "scan",
        "arrow",
        "arrows",
        "arrowhead",
        "arrowheads",
        "label",
        "labels",
        "labelled",
        "labeled",
        "panel",
        "panels",
        "inset",
        "circled",
        "indicated",
    ],
)
def test_every_visual_cue_fires_on_its_own(cue):
    assert classify(f"What is the {cue} here?", []) == "FIGURE_DEPENDENT"


@pytest.mark.parametrize(
    "question",
    [
        "What is passive immunization?",
        "Describe prokaryotic cell structure.",
        "How does pulmonary cryptococcosis present on imaging?",
        "Why is l-cysteine important for recovering Legionella?",
        "What does the paracortex of a lymph node contain?",
    ],
)
def test_an_ordinary_question_is_never_figure_dependent(question):
    """Only the visual verdict is asserted: these must not be refused for lack of a vision path."""
    assert classify(question, [block()]) != "FIGURE_DEPENDENT"


def test_asking_what_the_literature_says_about_imaging_is_not_asking_to_read_an_image():
    """ "Presents on imaging" is answerable from a radiology description in the text."""
    question = "How does pulmonary cryptococcosis present on imaging?"
    assert classify(question, [block(), figure()]) != "FIGURE_DEPENDENT"


# ----------------------------------------------------------- the evidence shape no longer decides


def test_one_figure_anchor_among_textual_anchors_does_not_make_the_question_visual():
    """The measured defect: a caption ranking fifth refused a question its prose answered."""
    anchors = [block(), block(), block(), block(), figure()]
    assert classify(ORDINARY, anchors) == "ORDINARY_FACTUAL"


def test_a_question_cue_still_wins_over_readable_figure_evidence():
    anchors = [block(), block(), block(), block(), figure()]
    assert classify("What does the figure show?", anchors) == "FIGURE_DEPENDENT"


def test_all_visual_anchors_that_carry_text_are_still_readable_evidence():
    """Captions are source text. Being entirely figure-derived is not the same as unreadable."""
    anchors = [figure("FIGURE 7-3 Organization of the lymph node."), figure("Table 10-2")]
    assert classify(ORDINARY, anchors) == "ORDINARY_FACTUAL"


def test_all_visual_anchors_without_text_are_figure_dependent():
    """Nothing here can be read without the picture, so the question fails closed."""
    assert classify(ORDINARY, [silent_figure(), silent_figure()]) == "FIGURE_DEPENDENT"


def test_one_readable_anchor_is_enough_to_escape_the_floor():
    assert classify(ORDINARY, [silent_figure(), block()]) == "ORDINARY_FACTUAL"
    assert classify(ORDINARY, [silent_figure(), figure("A real caption.")]) == "ORDINARY_FACTUAL"


def test_whitespace_is_not_readable_text():
    assert classify(ORDINARY, [block(chunk_type="FIGURE_CONTEXT", text="   \n\t ")]) == (
        "FIGURE_DEPENDENT"
    )


def test_the_floor_uses_the_same_visual_definition_as_claim_verification():
    """A block flagged `requires_visual_evidence` counts even if its chunk type does not say so."""
    odd = block(chunk_type="TEXT_CHILD", text="", requires_visual_evidence=True)
    assert visual(odd)
    assert classify(ORDINARY, [odd]) == "FIGURE_DEPENDENT"


def test_an_unknown_chunk_type_is_not_treated_as_visual():
    """Fail closed: an unrecognised type cannot satisfy the all-visual floor."""
    unknown = block(chunk_type="SOMETHING_NEW", text="", requires_visual_evidence=False)
    assert not visual(unknown)
    assert classify(ORDINARY, [unknown]) == "ORDINARY_FACTUAL"


def test_an_empty_anchor_set_keeps_its_previous_behaviour():
    """No anchors is not a visual problem; the gate refuses it as NO_EVIDENCE."""
    assert classify(ORDINARY, []) == "ORDINARY_FACTUAL"


def test_table_and_formula_fallbacks_are_unchanged():
    assert classify("What does it state?", [block(chunk_type="TABLE_PART")]) == "TABLE_DEPENDENT"
    assert classify("What does it state?", [block(chunk_type="FORMULA")]) == "FORMULA_DEPENDENT"
    assert classify("What does it state?", [block(chunk_type="QUESTION")]) == "ASSESSMENT"
    # A figure alongside a table no longer hides the table requirement.
    assert classify("What does it state?", [figure(), block(chunk_type="TABLE_PART")]) == (
        "TABLE_DEPENDENT"
    )


def test_helpers_are_honest_about_what_they_measure():
    assert visual(figure()) and visual(silent_figure())
    assert not visual(block())
    assert readable(figure()) and readable(block())
    assert not readable(silent_figure())


# ------------------------------------------------------------------------------- contract


def test_classification_is_deterministic():
    anchors = [block(), figure(), silent_figure()]
    assert len({classify(ORDINARY, anchors) for _ in range(25)}) == 1


def test_the_classifier_version_records_that_the_semantics_changed():
    assert CLASSIFIER_VERSION == "question-kind-v2"


# ------------------------------------------------------------------------------ the gate


def gate() -> SufficiencyGate:
    return SufficiencyGate(
        SufficiencyConfig(
            ordinary=EvidenceRequirement(),
            figure=EvidenceRequirement(require_visual_interpretation=True),
        )
    )


def evidence(blocks: list[EvidenceBlock]) -> EvidenceSet:
    return EvidenceSet(
        query_hash="h",
        retrieval_trace={},
        reranking_trace={},
        anchors=[b.anchor_chunk_id for b in blocks],
        expansions=[],
        evidence_blocks=blocks,
        total_tokens=sum(b.token_count for b in blocks),
        requires_visual_evidence=any(b.requires_visual_evidence for b in blocks),
        warnings=[],
        warning_details=[],
        duplicates_removed=0,
    )


def test_a_genuine_figure_question_still_abstains_before_generation():
    """Vision is not enabled by this change and a real visual question is still refused."""
    decision = gate().evaluate("What does Figure 12 show?", evidence([block(), figure()]))
    assert decision.status == "INSUFFICIENT"
    assert "VISUAL_INTERPRETATION_UNAVAILABLE" in decision.reason_codes
    assert "visual_interpretation" in decision.missing_requirements


def test_unreadable_evidence_still_abstains_before_generation():
    decision = gate().evaluate(ORDINARY, evidence([silent_figure(), silent_figure()]))
    assert decision.status == "INSUFFICIENT"
    assert "VISUAL_INTERPRETATION_UNAVAILABLE" in decision.reason_codes


def test_an_ordinary_question_with_a_figure_anchor_can_now_be_sufficient():
    decision = gate().evaluate(ORDINARY, evidence([block(), block(), figure()]))
    assert decision.status == "SUFFICIENT"
    assert "VISUAL_INTERPRETATION_UNAVAILABLE" not in decision.reason_codes


@pytest.mark.parametrize(
    "blocks,expected",
    [
        ([block(), block()], 0),
        ([block(), figure()], 1),
        ([figure(), figure(), block()], 2),
    ],
)
def test_the_visual_anchor_count_is_reported_either_way(blocks, expected):
    decision = gate().evaluate(ORDINARY, evidence(blocks))
    signal = next(s for s in decision.evaluated_signals if s.name == "figure_anchors_present")
    assert signal.value == expected
    assert signal.satisfied is True, "the count is a diagnostic, never a requirement"
