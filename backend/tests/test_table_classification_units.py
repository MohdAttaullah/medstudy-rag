"""A question is about a table when it asks about one, not when one happens to rank.

Two defects, both measured on the real corpus. `cell` was a table cue, and in a medical corpus that
is ordinary subject vocabulary — 920 of 3,939 indexed chunks contain it, against at most 3 for every
other cue — so five of six ordinary microbiology questions were refused with
TABLE_STRUCTURE_INCOMPLETE on evidence holding no table at all. And the structural fallback made 7
of 8 real TABLE_DEPENDENT verdicts come from what ranked rather than what was asked.

Tables get no structural floor, unlike figures: the builder renders caption, header rows and cells
into the chunk's own text, so a headerless table is a readable label/value layout rather than
unreadable evidence. M8 still refuses any claim citing a table whose artifact lacks header rows —
that is unchanged and is what keeps this safe. See ADR-024.
"""

from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.evidence.model import ArtifactRef, EvidenceBlock, EvidenceSet
from app.sufficiency.gate import SufficiencyGate
from app.sufficiency.question import CLASSIFIER_VERSION, TABLE_CUES, classify

ORDINARY = "What is passive immunization?"


def table_artifact(header_rows: list[int] | None = None) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=uuid4(),
        kind="TABLE",
        source_element_id=uuid4(),
        href="/a",
        row_indexes=[1, 2],
        header_rows=[0] if header_rows is None else header_rows,
    )


def block(
    *,
    chunk_type: str = "TEXT_CHILD",
    text: str = "Passive immunization provides preformed antibody.",
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
        artifacts=artifacts if artifacts is not None else [],
        question=None,
        expansion_reason="RERANKED_ANCHOR",
        context_reasons=["RERANKED_ANCHOR"],
        token_count=12,
        requires_visual_evidence=False,
    )


def table_block(header_rows: list[int] | None = None, kind: str = "TABLE") -> EvidenceBlock:
    return block(
        chunk_type=kind,
        text="Organism | Susceptibility\nC. albicans | ++++",
        artifacts=[table_artifact(header_rows)],
    )


# ------------------------------------------------------------------ the question decides


@pytest.mark.parametrize("cue", sorted(TABLE_CUES))
def test_every_retained_cue_fires_on_its_own(cue):
    assert classify(f"What is in the {cue} here?", []) == "TABLE_DEPENDENT"


def test_the_cue_vocabulary_is_exactly_the_agreed_set():
    assert TABLE_CUES == frozenset(
        {"table", "tabulated", "row", "rows", "column", "columns", "grid"}
    )


@pytest.mark.parametrize(
    "question",
    [
        "Describe prokaryotic cell structure.",
        "What is the function of the bacterial cell wall?",
        "How does a T cell recognise antigen?",
        "What is cell-mediated immunity?",
        "What happens to the host cell during lytic viral replication?",
        "Which organisms lack a cell wall?",
        "What is a sickle cell?",
        "Which cells produce defensins?",
    ],
)
def test_an_ordinary_biological_cell_question_is_not_table_dependent(question):
    """`cell` is subject vocabulary here; it was refusing core microbiology questions."""
    assert classify(question, []) != "TABLE_DEPENDENT"


@pytest.mark.parametrize(
    "question",
    [
        "According to Table 7-3, what is the normal neutrophil count?",
        "Which row lists the dose?",
        "Compare the columns of the susceptibility table.",
        "What are the tabulated values?",
        "Which cell of the table contains the MIC?",
    ],
)
def test_an_explicit_table_question_is_still_table_dependent(question):
    """Including the one that says "cell" — a question about a table cell says "table"."""
    assert classify(question, []) == "TABLE_DEPENDENT"


# ------------------------------------------------------------- the evidence no longer decides


@pytest.mark.parametrize("kind", ["TABLE", "TABLE_PART"])
def test_a_table_anchor_without_a_cue_does_not_change_the_question(kind):
    """7 of 8 real verdicts came from this path rather than from the question."""
    assert classify(ORDINARY, [table_block(kind=kind)]) == "ORDINARY_FACTUAL"
    assert classify(ORDINARY, [block(), table_block(kind=kind)]) == "ORDINARY_FACTUAL"


def test_even_an_all_table_anchor_set_does_not_change_the_question():
    """No structural floor for tables: the chunk renders its own headers and rows as text."""
    assert classify(ORDINARY, [table_block(), table_block()]) == "ORDINARY_FACTUAL"


def test_a_headerless_table_anchor_still_does_not_change_the_question():
    assert classify(ORDINARY, [table_block(header_rows=[])]) == "ORDINARY_FACTUAL"


def test_a_table_cue_still_wins_over_prose_evidence():
    assert classify("Which row of the table lists it?", [block()]) == "TABLE_DEPENDENT"


def test_formula_and_assessment_fallbacks_are_unchanged():
    assert classify("What does it state?", [block(chunk_type="FORMULA")]) == "FORMULA_DEPENDENT"
    assert classify("What does it state?", [block(chunk_type="QUESTION")]) == "ASSESSMENT"


def test_a_figure_cue_still_wins_over_a_table_anchor():
    assert classify("What does the figure show?", [table_block()]) == "FIGURE_DEPENDENT"


def test_an_empty_anchor_set_is_unchanged():
    assert classify(ORDINARY, []) == "ORDINARY_FACTUAL"


def test_classification_is_deterministic():
    anchors = [block(), table_block(), table_block(header_rows=[])]
    assert len({classify(ORDINARY, anchors) for _ in range(25)}) == 1


def test_the_classifier_version_records_that_the_semantics_changed():
    assert CLASSIFIER_VERSION == "question-kind-v3"


# --------------------------------------------------------------------------------- the gate


def gate() -> SufficiencyGate:
    return SufficiencyGate(
        SufficiencyConfig(
            ordinary=EvidenceRequirement(),
            table=EvidenceRequirement(require_table_structure=True),
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
        requires_visual_evidence=False,
        warnings=[],
        warning_details=[],
        duplicates_removed=0,
    )


TABLE_QUESTION = "Which row of the table lists the dose?"


def test_a_genuine_table_question_with_a_headerless_table_still_blocks():
    decision = gate().evaluate(TABLE_QUESTION, evidence([table_block(header_rows=[])]))
    assert decision.status == "INSUFFICIENT"
    assert "TABLE_STRUCTURE_INCOMPLETE" in decision.reason_codes


def test_a_genuine_table_question_with_no_table_at_all_still_blocks():
    """The C1 shape: the question names a table and retrieval returned none."""
    decision = gate().evaluate(TABLE_QUESTION, evidence([block(), block()]))
    assert decision.status == "INSUFFICIENT"
    assert "TABLE_STRUCTURE_INCOMPLETE" in decision.reason_codes


def test_a_genuine_table_question_with_headers_is_satisfied():
    decision = gate().evaluate(TABLE_QUESTION, evidence([table_block()]))
    assert decision.status == "SUFFICIENT"
    assert "REQUIRED_ARTIFACT_PRESENT" in decision.reason_codes


def test_an_ordinary_question_may_proceed_despite_a_table_anchor():
    decision = gate().evaluate(ORDINARY, evidence([block(), table_block(header_rows=[])]))
    assert decision.status == "SUFFICIENT"
    assert "TABLE_STRUCTURE_INCOMPLETE" not in decision.reason_codes


@pytest.mark.parametrize(
    "blocks,expected",
    [
        ([block(), block()], 0),
        ([block(), table_block()], 1),
        ([table_block(), table_block(kind="TABLE_PART")], 2),
    ],
)
def test_the_table_anchor_count_is_reported_either_way(blocks, expected):
    decision = gate().evaluate(ORDINARY, evidence(blocks))
    signal = next(s for s in decision.evaluated_signals if s.name == "table_anchors_present")
    assert signal.value == expected
    assert signal.satisfied is True, "the count is a diagnostic, never a requirement"
