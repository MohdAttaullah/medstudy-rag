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

CLASSIFIER_VERSION: Literal["question-kind-v1"] = "question-kind-v1"

# A fixed tokenization for matching words, deliberately not the tenant's active index analyzer:
# M7 must not change its behaviour because a lexical index was rebuilt, and reading M5's policy
# here would make that happen. Nothing in this module touches an index.
ANALYZER = SparseAnalyzerConfig()

# Cues are matched against analyzer terms, so biomedical identifiers stay intact and the match is
# whole-term rather than substring: "table" must not fire on "acceptable".
TABLE_CUES = frozenset({"table", "tabulated", "row", "rows", "column", "columns", "cell", "grid"})
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
        "photograph",
        "micrograph",
        "scan",
    }
)

# Structural fallback: what the ranked anchors actually are.
TABLE_CHUNKS = frozenset({"TABLE", "TABLE_PART"})
FORMULA_CHUNKS = frozenset({"FORMULA"})
FIGURE_CHUNKS = frozenset({"FIGURE_CONTEXT"})
QUESTION_CHUNKS = frozenset({"QUESTION"})


def classify(question: str, anchors: list[EvidenceBlock]) -> QuestionKind:
    """Return the evidence structure this question requires.

    The question's own wording wins over the retrieved shape: asking "which row of the table" needs
    table structure even if retrieval happened to return prose, and that mismatch must surface as
    insufficiency rather than be classified away.
    """
    asked = set(terms(question, ANALYZER))
    if asked & FIGURE_CUES:
        return "FIGURE_DEPENDENT"
    if asked & TABLE_CUES:
        return "TABLE_DEPENDENT"
    if asked & FORMULA_CUES:
        return "FORMULA_DEPENDENT"
    kinds = {block.chunk_type for block in anchors}
    if kinds & FIGURE_CHUNKS:
        return "FIGURE_DEPENDENT"
    if kinds & TABLE_CHUNKS:
        return "TABLE_DEPENDENT"
    if kinds & FORMULA_CHUNKS:
        return "FORMULA_DEPENDENT"
    if kinds and kinds <= QUESTION_CHUNKS:
        return "ASSESSMENT"
    return "ORDINARY_FACTUAL"
