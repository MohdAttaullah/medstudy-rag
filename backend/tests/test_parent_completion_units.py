"""A required parent is admitted as the completion of its anchor, not as a whole parent chunk.

Chunking builds parents to ~1280 tokens; a required expansion may admit 512. Asking for the whole
uncovered parent therefore failed almost always: of 130 real reranker anchors, 46 required a
parent, 45 were offered one and lost it to the budget, and 2 parents were admitted in total.

What a mid-sentence anchor actually needs is much smaller — the text immediately following it, up
to the end of that sentence. These tests pin that selection, the ordering that reserves budget for
it before optional context, and the fail-closed behaviour when the completion genuinely does not
fit. Nothing is truncated mid-sentence and no continuation text is invented.
"""

from uuid import UUID, uuid4, uuid5

import pytest
from app.core.reranking_config import EvidenceBudgetConfig, ExpansionConfig
from app.evidence.assembly import EvidenceAssembler, _sentence_end
from app.evidence.model import EvidenceSource, SourceSpan
from app.retrieval.model import Provenance

TENANT = uuid4()
VERSION = uuid4()
RUN = uuid4()
DOCUMENT = uuid4()
ELEMENT = uuid5(UUID(int=3), "element")


def words(count: int, word: str = "context") -> str:
    return " ".join([word] * count)


def span(text: str, start: int = 0, reading_order: int = 0, element: UUID = ELEMENT) -> SourceSpan:
    return SourceSpan(
        element_id=element,
        start=start,
        end=start + len(text),
        text=text,
        page=13,
        reading_order=reading_order,
        role="BODY",
        bbox=(None, None, None, None),
    )


def source(
    text: str,
    *,
    spans: list[SourceSpan] | None = None,
    chunk_type: str = "TEXT_CHILD",
    parent: UUID | None = None,
    chunk_id: UUID | None = None,
    sequence: int = 1,
) -> EvidenceSource:
    return EvidenceSource(
        tenant_id=TENANT,
        chunk_id=chunk_id or uuid4(),
        source_chunk_ids=[],
        parse_run_id=RUN,
        provenance=Provenance(
            document_id=DOCUMENT,
            document_version_id=VERSION,
            chunk_run_id=RUN,
            document_title="Medical Microbiology",
            source_type="TEXTBOOK",
            authority_level="REFERENCE",
            subject=None,
            specialty=None,
            chunk_type=chunk_type,
            page_start=13,
            page_end=13,
            sequence_number=sequence,
            parent_chunk_id=parent,
            question_id=None,
        ),
        retrieval_text=text,
        evidence_text=text,
        text=text,
        spans=spans if spans is not None else [span(text)],
        artifacts=[],
        question=None,
        hierarchy=[],
    )


def assembler(**overrides) -> EvidenceAssembler:
    expansion = ExpansionConfig(**overrides.pop("expansion", {}))
    budget = EvidenceBudgetConfig(**overrides.pop("budget", {}))
    return EvidenceAssembler(expansion, budget, lambda text: len(text.split()))


def build(anchor: EvidenceSource, parent: EvidenceSource | None = None, **overrides):
    relatives = {anchor.chunk_id: [parent]} if parent else {}
    return assembler(**overrides).assemble([anchor], relatives)


FRAGMENT = "Legionellae require l-cysteine and"


def scenario(
    before: str = "", after: str = " recovery improves markedly.", text: str = FRAGMENT
) -> tuple[EvidenceSource, EvidenceSource]:
    """An anchor sitting at its real offset inside its parent element.

    Offsets matter: `remaining` subtracts the anchor's interval from the parent by position, so a
    fixture whose anchor claims offset 0 while its text sits mid-element would test nothing real.
    """
    whole = f"{before}{text}{after}"
    parent_id = uuid4()
    anchor = source(text, spans=[span(text, start=len(before))], parent=parent_id, sequence=1)
    parent = source(
        whole,
        spans=[span(whole, start=0)],
        chunk_type="TEXT_PARENT",
        chunk_id=parent_id,
        sequence=0,
    )
    return anchor, parent


def anchor_fragment(text: str = FRAGMENT) -> EvidenceSource:
    return source(text, parent=uuid4())


def parent_blocks(blocks):
    return [b for b in blocks if b.expansion_reason == "PARENT_EXPANSION"]


def missing(warnings):
    return [w for w in warnings if w.code == "CONTEXT_REQUIRED_PARENT_MISSING"]


# ------------------------------------------------------------------- sentence boundary


@pytest.mark.parametrize(
    "text,expected",
    [
        ("finishes here. and more", 14),
        ("asks something? then more", 15),
        ("shouts! then more", 7),
        ("no sentence end at all", None),
        ("", None),
    ],
)
def test_the_sentence_boundary_is_the_first_terminator(text, expected):
    assert _sentence_end(text) == expected


# ------------------------------------------------------------- completion selection


def test_a_huge_parent_yields_a_short_required_completion():
    """The real shape: a ~1280-token parent, a completion of a few words."""
    anchor, parent = scenario(
        before=words(600) + " ", after=" recovery improves markedly. " + words(600)
    )
    blocks, warnings, _ = build(anchor, parent)

    admitted = parent_blocks(blocks)
    assert admitted, "the completion must be admitted"
    assert not missing(warnings)
    assert admitted[0].token_count <= ExpansionConfig().max_parent_tokens
    assert admitted[0].token_count < 20, "only the completing sentence, not the whole parent"
    assert admitted[0].text.strip() == "recovery improves markedly."


def test_text_before_the_anchor_is_not_part_of_the_completion():
    """Preceding material is surrounding context, not what finishes the sentence."""
    anchor, parent = scenario(before="EARLIER MATERIAL. ", after=" recovery improves. ")
    blocks, _, _ = build(anchor, parent)
    admitted = parent_blocks(blocks)
    assert admitted
    assert "EARLIER MATERIAL" not in admitted[0].text


def test_a_completion_within_the_limit_is_admitted():
    anchor, parent = scenario(after=" " + words(100) + ".")
    blocks, warnings, _ = build(anchor, parent, expansion={"max_parent_tokens": 512})
    assert parent_blocks(blocks)
    assert not missing(warnings)


def test_a_genuine_completion_over_the_limit_blocks():
    """No truncation: an over-long completion abstains rather than being cut to fit."""
    anchor, parent = scenario(after=" " + words(900) + ".")
    blocks, warnings, _ = build(anchor, parent, expansion={"max_parent_tokens": 512})
    assert not parent_blocks(blocks)
    assert missing(warnings), "an unmet required completion must be reported"
    assert missing(warnings)[0].required_dependency is True


def test_an_absent_parent_blocks():
    anchor = anchor_fragment()
    blocks, warnings, _ = build(anchor, None)
    assert not parent_blocks(blocks)
    assert missing(warnings)


def test_a_complete_anchor_needs_no_parent():
    """An anchor ending a sentence is not a fragment; no completion is required or admitted."""
    anchor, parent = scenario(
        after=" More follows.", text="Legionellae require l-cysteine for recovery."
    )
    blocks, warnings, _ = build(anchor, parent)
    assert not missing(warnings)
    assert not parent_blocks(blocks), "a complete anchor pulls in no required parent"


def test_a_parent_offering_nothing_after_the_anchor_blocks():
    """Nothing follows, so nothing can complete it; fail closed rather than invent text."""
    anchor, parent = scenario(before=words(50) + " ", after="")
    blocks, warnings, _ = build(anchor, parent)
    assert not parent_blocks(blocks)
    assert missing(warnings)


# --------------------------------------------------------------------------- ordering


def test_an_optional_sibling_cannot_starve_a_required_parent():
    """Pass 2 reserves budget for completions before pass 3 spends any on siblings.

    The sibling here is large enough that, admitted first, it would leave no room for the
    completion that the anchor actually needs.
    """
    anchor, parent = scenario(after=" recovery improves markedly.")
    sibling = source(
        words(60) + ".",
        chunk_type="TEXT_CHILD",
        parent=anchor.provenance.parent_chunk_id,
        sequence=2,
    )
    relatives = {anchor.chunk_id: [sibling, parent]}
    blocks, warnings, _ = assembler(budget={"max_total_tokens": 80}).assemble([anchor], relatives)

    assert parent_blocks(blocks), "the required completion must win the budget"
    assert not missing(warnings)


def test_the_parent_is_never_admitted_as_optional_context():
    anchor, parent = scenario(after=" trailing material.", text="Complete sentence here.")
    blocks, _, _ = build(anchor, parent)
    assert not parent_blocks(blocks)


# ------------------------------------------------------------------------ provenance


def test_the_completion_keeps_exact_source_span_provenance():
    """A cut span stays the same element with narrowed offsets and matching text."""
    anchor, parent = scenario(after=" recovery improves. Later material.")
    blocks, _, _ = build(anchor, parent)
    admitted = parent_blocks(blocks)[0]

    assert admitted.pages == [13]
    assert admitted.document_version_id == VERSION
    assert admitted.parse_run_id == RUN
    for recorded in admitted.source_spans:
        assert recorded.element_id == ELEMENT
        assert recorded.end - recorded.start == len(recorded.text)
        assert parent.text[recorded.start : recorded.end] == recorded.text


def test_already_covered_text_is_not_duplicated():
    """The anchor's own text is in the set already; the completion must not repeat it."""
    anchor, parent = scenario(after=" recovery improves.")
    blocks, _, _ = build(anchor, parent)
    admitted = parent_blocks(blocks)[0]
    assert anchor.text not in admitted.text


def test_assembly_is_deterministic():
    anchor, parent = scenario(before=words(30) + " ", after=" recovery improves. " + words(30))
    first, first_warnings, _ = build(anchor, parent)
    second, second_warnings, _ = build(anchor, parent)
    assert [b.text for b in first] == [b.text for b in second]
    assert [b.evidence_id for b in first] == [b.evidence_id for b in second]
    assert [w.code for w in first_warnings] == [w.code for w in second_warnings]


# ------------------------------------------------------------- integrity unchanged


def test_a_cross_run_relative_still_raises():
    """The hard lineage failure is untouched by the new ordering."""
    from app.reranking.model import RerankingError

    anchor = anchor_fragment()
    stranger = source(
        "Other run.", chunk_type="TEXT_PARENT", chunk_id=anchor.provenance.parent_chunk_id
    )
    object.__setattr__(stranger, "tenant_id", uuid4())
    with pytest.raises(RerankingError, match="CONTEXT_SOURCE_LINEAGE_MISMATCH"):
        assembler().assemble([anchor], {anchor.chunk_id: [stranger]})
