"""Polarity belongs to a clause, not to whichever sentence happens to contain a negation.

Measured on a real corpus. The source says:

    "On the tentorial surface, the transition from the vermis to the hemispheres is smooth and not
     marked by the deep fissures on the suboccipital surface between the vermis and hemispheres."

A draft quoting the positive half — "The transition from vermis to hemispheres is smooth." — was
reported NEGATION_REVERSED and failed as CONTRADICTED, because the check asked whether the
*sentence* carried a negation. It did; the claim's own clause did not. Half of ten runs of a
plainly supported question abstained on this.

What must still fire: a claim that asserts the predicate the evidence negates.
"""

from uuid import uuid4

import pytest
from app.evidence.model import EvidenceBlock, SourceSpan
from app.verification.claims import ANALYZER  # noqa: F401  (pins the analyzer this check uses)
from app.verification.deterministic import check_negation
from app.verification.model import Claim


def block(text: str) -> EvidenceBlock:
    element = uuid4()
    return EvidenceBlock(
        evidence_id=uuid4(),
        anchor_chunk_id=uuid4(),
        source_chunk_ids=[uuid4()],
        source_element_ids=[element],
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_run_id=uuid4(),
        parse_run_id=uuid4(),
        document_title="Synthetic reference",
        source_type="TEXTBOOK",
        authority_level="REFERENCE",
        chunk_type="TEXT_CHILD",
        pages=[3],
        hierarchy=[],
        source_spans=[
            SourceSpan(
                element_id=element,
                start=0,
                end=len(text),
                text=text,
                page=3,
                reading_order=0,
                role="PRIMARY",
                bbox=(None, None, None, None),
            )
        ],
        text=text,
        requires_visual_evidence=False,
        question=None,
        artifacts=[],
        representation="source-spans-v1",
        expansion_reason="ANCHOR",
        token_count=len(text.split()),
    )


def claim(text: str) -> Claim:
    return Claim(
        claim_id=uuid4(),
        text=text,
        start=0,
        end=len(text),
        claim_type="FACTUAL",
        material=True,
        cited_evidence_ids=[],
        context=text,
    )


SURFACE = (
    "On the tentorial surface, the transition from the vermis to the hemispheres is smooth and "
    "not marked by the deep fissures on the suboccipital surface between the vermis and "
    "hemispheres."
)


def test_quoting_the_positive_half_of_a_mixed_sentence_is_not_a_reversal():
    """The defect itself, in the words the corpus actually uses."""
    assert (
        check_negation(
            claim("The transition from the vermis to the hemispheres is smooth."), [block(SURFACE)]
        )
        == []
    )


def test_quoting_the_negated_half_is_not_a_reversal_either():
    assert (
        check_negation(
            claim("The transition is not marked by the deep fissures on the suboccipital surface."),
            [block(SURFACE)],
        )
        == []
    )


@pytest.mark.parametrize(
    "evidence,asserted",
    [
        (
            "Drug A is not indicated in pregnancy.",
            "Drug A is indicated in pregnancy.",
        ),
        (
            "The posterior incisura is never occupied by the brainstem.",
            "The posterior incisura is occupied by the brainstem.",
        ),
        (
            "Concurrent use is contraindicated in renal impairment.",
            "Concurrent use is indicated in renal impairment.",
        ),
    ],
)
def test_a_real_reversal_still_fires(evidence, asserted):
    """The check exists for exactly these, and clause-level comparison does not soften them."""
    assert check_negation(claim(asserted), [block(evidence)]) == ["NEGATION_REVERSED"]


def test_a_reversal_inside_a_coordinated_sentence_still_fires():
    """The negated clause is still weighed on its own, so a reversal of it is still caught."""
    evidence = "Drug A lowers blood pressure and is not safe in pregnancy."
    assert check_negation(claim("Drug A is safe in pregnancy."), [block(evidence)]) == [
        "NEGATION_REVERSED"
    ]


def test_a_claim_about_an_unrelated_subject_is_not_judged():
    """Polarity is only compared against passages about the same subject."""
    assert check_negation(claim("Warfarin is metabolised by CYP2C9."), [block(SURFACE)]) == []


def test_a_negation_spanning_a_coordination_is_matched_by_the_whole_sentence():
    """ "not A and B" negates both halves, and the sentence stays a candidate passage for that."""
    evidence = "The vermis is not continuous with the hemispheres and the tonsils."
    assert (
        check_negation(
            claim("The vermis is not continuous with the hemispheres and the tonsils."),
            [block(evidence)],
        )
        == []
    )
