"""M8's figure rule is what makes relaxing the question classifier safe, so it is pinned here.

The gate no longer refuses a whole question because a caption ranked among the evidence. That is
only defensible because the claim-level rule is untouched: a claim citing a figure block fails,
deterministically, whatever the question was classified as and whatever a model asserts. These
tests exist so a future change to `classify` cannot quietly remove the thing standing behind it.

Nothing here is new behaviour. Every assertion describes M8 exactly as it was before ADR-023.
"""

from uuid import uuid4

import pytest
from app.core.verification_config import ClaimVerificationConfig
from app.evidence.model import ArtifactRef, EvidenceBlock, SourceSpan
from app.verification.claims import classify
from app.verification.deterministic import check_structured_evidence
from app.verification.engine import deterministic_pass
from app.verification.model import Claim

TENANT = uuid4()


ANTIBODY = "Preformed antibody is given."


def block(*, chunk_type: str = "TEXT_CHILD", text: str = ANTIBODY) -> EvidenceBlock:
    anchor = uuid4()
    # Real spans: `_provenance_resolves` requires them, so a block without any fails citation
    # provenance for a reason unrelated to what these tests are about.
    element = uuid4()
    spans = [
        SourceSpan(
            element_id=element,
            start=0,
            end=len(text),
            text=text,
            page=13,
            reading_order=0,
            role="BODY",
            bbox=(None, None, None, None),
        )
    ]
    artifacts = (
        [
            ArtifactRef(
                artifact_id=uuid4(),
                kind="FIGURE",
                source_element_id=uuid4(),
                href="/a",
                image_available=True,
            )
        ]
        if chunk_type == "FIGURE_CONTEXT"
        else []
    )
    return EvidenceBlock(
        evidence_id=uuid4(),
        anchor_chunk_id=anchor,
        source_chunk_ids=[anchor],
        source_element_ids=[element],
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
        source_spans=spans,
        text=text,
        representation="m3-source-with-structural-labels-v1",
        trimmed_text_present_elsewhere=True,
        artifacts=artifacts,
        question=None,
        expansion_reason="RERANKED_ANCHOR",
        context_reasons=["RERANKED_ANCHOR"],
        token_count=12,
        requires_visual_evidence=chunk_type == "FIGURE_CONTEXT",
    )


def claim(cited: list[EvidenceBlock], text: str = ANTIBODY) -> Claim:
    return Claim(
        claim_id=uuid4(),
        text=text,
        claim_type=classify(text, cited, True),
        material=True,
        cited_evidence_ids=[b.evidence_id for b in cited],
        start=0,
        end=len(text),
    )


def codes(cited: list[EvidenceBlock]) -> list[str]:
    return check_structured_evidence(claim(cited), cited)


# ------------------------------------------------------------- the rule that carries the safety


def test_a_claim_citing_a_figure_block_fails():
    figure = block(chunk_type="FIGURE_CONTEXT", text="FIGURE 11-1 Types of immunizations.")
    assert "VISUAL_INTERPRETATION_REQUIRED" in codes([figure])


def test_a_claim_citing_text_and_a_figure_together_still_fails():
    """Mixed citation is not a way round the rule, even when the text alone would support it."""
    assert "VISUAL_INTERPRETATION_REQUIRED" in codes([block(), block(chunk_type="FIGURE_CONTEXT")])


def test_a_readable_caption_does_not_earn_a_claim_its_release():
    """Caption text is visible to the generator; it is still not support for a released claim."""
    rich = block(
        chunk_type="FIGURE_CONTEXT",
        text=(
            "FIGURE 7-3 Organization of the lymph node. Beneath the capsule is the subcapsular "
            "sinus, which is lined with phagocytic cells."
        ),
    )
    assert "VISUAL_INTERPRETATION_REQUIRED" in codes([rich])


def test_a_claim_citing_only_text_passes_the_structured_check():
    assert codes([block(), block()]) == []


def test_an_uncited_figure_elsewhere_in_the_set_does_not_taint_a_textual_claim():
    """The change this pins: a figure in the EvidenceSet that no claim cites is simply context."""
    text_blocks = [block(), block()]
    unrelated = block(chunk_type="FIGURE_CONTEXT", text="FIGURE 12-1 Major features.")
    supplied = {str(b.evidence_id): b for b in [*text_blocks, unrelated]}

    found, cited = deterministic_pass(
        claim(text_blocks), supplied, TENANT, ClaimVerificationConfig()
    )
    assert "VISUAL_INTERPRETATION_REQUIRED" not in found
    assert unrelated not in cited


def test_the_claim_type_for_a_figure_citation_is_unchanged():
    figure = block(chunk_type="FIGURE_CONTEXT")
    assert classify("Preformed antibody is given.", [figure], True) == "VISUAL_DEPENDENT"


def test_a_block_flagged_visual_without_the_figure_chunk_type_still_fails():
    """The rule is a disjunction; neither half may be dropped."""
    odd = block(text="Something.")
    flagged = odd.model_copy(update={"requires_visual_evidence": True})
    assert "VISUAL_INTERPRETATION_REQUIRED" in codes([flagged])


# ------------------------------------------------- the table rule, unchanged by ADR-024 too


def table_block(header_rows: list[int], kind: str = "TABLE") -> EvidenceBlock:
    b = block(chunk_type=kind, text="Organism | Susceptibility / C. albicans | ++++")
    return b.model_copy(
        update={
            "artifacts": [
                ArtifactRef(
                    artifact_id=uuid4(),
                    kind="TABLE",
                    source_element_id=uuid4(),
                    href="/a",
                    row_indexes=[1],
                    header_rows=header_rows,
                )
            ]
        }
    )


def test_a_claim_citing_a_headerless_table_still_fails():
    """The classifier no longer asks about tables; this is what still protects them."""
    assert "PROVENANCE_UNRESOLVED" in codes([table_block(header_rows=[])])


def test_a_claim_citing_a_table_with_headers_passes():
    assert codes([table_block(header_rows=[0])]) == []


def test_a_claim_citing_text_and_a_headerless_table_still_fails():
    assert "PROVENANCE_UNRESOLVED" in codes([block(), table_block(header_rows=[])])


def test_an_uncited_headerless_table_elsewhere_does_not_taint_a_textual_claim():
    text_blocks = [block(), block()]
    unrelated = table_block(header_rows=[])
    supplied = {str(b.evidence_id): b for b in [*text_blocks, unrelated]}
    found, cited = deterministic_pass(
        claim(text_blocks), supplied, TENANT, ClaimVerificationConfig()
    )
    assert "PROVENANCE_UNRESOLVED" not in found
    assert unrelated not in cited


@pytest.mark.parametrize("count", [1, 2, 3])
def test_any_figure_among_the_cited_blocks_is_enough_to_fail(count):
    cited = [block() for _ in range(4)] + [block(chunk_type="FIGURE_CONTEXT") for _ in range(count)]
    assert "VISUAL_INTERPRETATION_REQUIRED" in codes(cited)
