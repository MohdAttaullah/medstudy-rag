"""Deterministic question classification.

No model, no rewriting, no paraphrase: a fixed versioned cue table over the user's own words, with
the EvidenceSet's own structural types as the fallback. The classification only ever *raises* what
the evidence must contain, so a misclassification abstains rather than answering from less.
"""

from typing import Literal

from app.core.generation_config import QuestionKind
from app.core.retrieval_config import SparseAnalyzerConfig
from app.evidence.model import EvidenceBlock
from app.retrieval.sparse.analyzer import terms

CLASSIFIER_VERSION: Literal["question-kind-v3"] = "question-kind-v3"

# A fixed tokenization for matching words, deliberately not the tenant's active index analyzer:
# M7 must not change its behaviour because a lexical index was rebuilt, and reading M5's policy
# here would make that happen. Nothing in this module touches an index.
ANALYZER = SparseAnalyzerConfig()

# Cues are matched against analyzer terms, so biomedical identifiers stay intact and the match is
# whole-term rather than substring: "table" must not fire on "acceptable".
TABLE_CUES = frozenset({"table", "tabulated", "row", "rows", "column", "columns", "grid"})

#: Deliberately absent from the table cues: "cell". In a medical corpus it is ordinary subject
#: vocabulary — cell wall, cell membrane, T cell, cell-mediated immunity, host cell — occurring in
#: 920 of 3,939 indexed chunks (23.4%), where every other table cue occurs in at most 3. As a cue
#: it refused five of six ordinary microbiology questions with TABLE_STRUCTURE_INCOMPLETE on
#: evidence that contained no table at all. Genuine table questions are unaffected: a question
#: about a table cell says "table". See ADR-024.
FORMULA_CUES = frozenset(
    {
        "formula",
        "formulae",
        "equation",
        "equations",
        "calculate",
        "calculated",
        "calculation",
        "compute",
        "computed",
        "derive",
        "derived",
        "expression",
    }
)
FIGURE_CUES = frozenset(
    {
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
        # Deixis *into* an image. "What structure is indicated by the arrow?" names no figure and
        # matched nothing before, yet it cannot be answered without reading the picture.
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
    }
)

#: Deliberately absent from the cues above: "imaging". "How does pulmonary cryptococcosis present
#: on imaging?" asks what the literature *describes* about radiological appearance, which the text
#: answers. Treating it as a request to read a picture would refuse a legitimately textual
#: question, which is the same over-reach this version exists to remove.

# Structural fallback: what the ranked anchors actually are.
TABLE_CHUNKS = frozenset({"TABLE", "TABLE_PART"})
FORMULA_CHUNKS = frozenset({"FORMULA"})
FIGURE_CHUNKS = frozenset({"FIGURE_CONTEXT"})
QUESTION_CHUNKS = frozenset({"QUESTION"})


def visual(block: EvidenceBlock) -> bool:
    """Whether this block is a figure whose meaning lives in an image.

    The same disjunction M8 uses in `verification/deterministic.py`, deliberately: if the gate and
    the claim checker disagreed about what counts as visual, one of them would be wrong about every
    block they disagreed on.
    """
    return block.chunk_type in FIGURE_CHUNKS or block.requires_visual_evidence


def readable(block: EvidenceBlock) -> bool:
    """Whether the block carries source text a reader could actually use.

    A FIGURE_CONTEXT chunk is the figure's caption and adjacent prose, not its pixels, so it is
    usually readable. M3 marks the other case at ingestion (`CHUNK_FIGURE_NO_TEXT`); here it is
    simply the absence of text.
    """
    return bool((block.text or "").strip())


def classify(question: str, anchors: list[EvidenceBlock]) -> QuestionKind:
    """Return the evidence structure this question requires.

    The question's own wording wins over the retrieved shape: asking "which row of the table" needs
    table structure even if retrieval happened to return prose, and that mismatch must surface as
    insufficiency rather than be classified away.

    Visual dependence is decided by the question, not by what happened to rank. A figure chunk
    among readable prose is context; it does not make the question about a picture. Measured on a
    real corpus, the previous rule — any FIGURE_CONTEXT anchor — classified 11 of 26 questions
    FIGURE_DEPENDENT, **none** of them from the question's own words, and because no vision path
    exists that meant 11 permanent abstentions with the answering passage sitting in the textual
    anchors. ADR-012 said "a figure question"; this restores that reading. See ADR-023.

    The one structural case that remains is genuine unreadability: if every anchor is a figure and
    none carries text, there is nothing to answer from without reading pixels. M8 remains the
    claim-level guarantee — any claim citing a figure block still fails there, unchanged.

    Table dependence is decided the same way, and for the same measured reason: 7 of the 8 real
    TABLE_DEPENDENT classifications came from a ranked table rather than from the question. Tables
    get no structural floor at all, because unlike a figure a table chunk is missing nothing — the
    builder renders its caption, header rows and cells into the chunk's own text, so a table
    without declared headers is a readable label/value layout rather than unreadable evidence.
    M8 still refuses any claim citing a table whose artifact lacks header rows. See ADR-024.
    """
    asked = set(terms(question, ANALYZER))
    if asked & FIGURE_CUES:
        return "FIGURE_DEPENDENT"
    if asked & TABLE_CUES:
        return "TABLE_DEPENDENT"
    if asked & FORMULA_CUES:
        return "FORMULA_DEPENDENT"
    if anchors and all(visual(b) and not readable(b) for b in anchors):
        return "FIGURE_DEPENDENT"
    kinds = {block.chunk_type for block in anchors}
    if kinds & FORMULA_CHUNKS:
        return "FORMULA_DEPENDENT"
    if kinds and kinds <= QUESTION_CHUNKS:
        return "ASSESSMENT"
    return "ORDINARY_FACTUAL"
