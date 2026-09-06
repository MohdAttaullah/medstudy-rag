"""Versioned biomedical lexical analysis.

This is the single definition of what a "term" is, for both the indexed corpus and the query. If
the two ever disagreed, exact-term retrieval would fail silently, so both sides call this module.

The design goal is narrow and testable: **do not destroy biomedical identifiers**. A naive
tokenizer splits on every non-alphanumeric character and turns `HLA-B27` into `hla` and `b27`,
`Na+/K+-ATPase` into four unrelated fragments and `1.5` into `1` and `5`. Each of those is a
lexical match a clinician would expect to work, and losing them is precisely the failure BM25 is
in the pipeline to prevent.

Nothing here expands, rewrites or interprets text. There is no synonym list, no abbreviation
dictionary and no stemmer: M5 measures whether plain lexical retrieval works before any query
manipulation is layered over it.
"""

import re
import unicodedata
from collections import Counter

from app.core.retrieval_config import SparseAnalyzerConfig

# A token starts with a letter or digit and may then carry the connectors that hold biomedical
# identifiers together. Underscore is excluded so it behaves as a separator, and the connector
# set deliberately includes `+`, `-`, `/`, `.` and `%` because they carry meaning in this domain.
TOKEN = re.compile(r"[^\W_][^\W_]*(?:[-+/._'%][^\W_]*)*", re.UNICODE)
# Trailing sentence punctuation is not part of the term; a trailing `+` or `%` is (`Na+`, `70%`).
TRAILING = "-/._'"
SPLIT = re.compile(r"[-+/._']")
# Marks a case-sensitive term so it occupies its own key space. It cannot collide with a
# normalized term because `^` never appears inside a token.
EXACT_PREFIX = "^"

# Deterministic typographic folding applied before NFKC, so that a curly quote or an en dash in a
# parsed PDF produces the same term as its ASCII equivalent in a typed query.
PUNCTUATION = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    " ": " ",
    "⁄": "/",
}


def fold(text: str) -> str:
    """Deterministic character-level normalization shared by the corpus and the query."""
    folded = "".join(PUNCTUATION.get(character, character) for character in text)
    return unicodedata.normalize("NFKC", folded)


def _parts(token: str, config: SparseAnalyzerConfig) -> list[str]:
    """Letter-bearing components of a connected identifier.

    `hla-b27` yields `hla` and `b27`, so a query naming only the gene still matches, while the
    whole term remains available for an exact identifier match. Single-character fragments are
    dropped: they carry almost no information and would dominate the postings.
    """
    if config.compound_policy != "WHOLE_AND_PARTS":
        return []
    found = []
    for piece in SPLIT.split(token):
        piece = piece.strip("+%")
        if len(piece) >= 2 and piece != token and piece not in found:
            found.append(piece)
    return found


def terms(text: str, config: SparseAnalyzerConfig) -> list[str]:
    """The ordered term stream for one text under this analyzer version."""
    produced: list[str] = []
    stopwords = frozenset(word.casefold() for word in config.stopwords)
    for match in TOKEN.finditer(fold(text)):
        surface = match.group().rstrip(TRAILING)
        if not surface:
            continue
        normalized = surface.casefold()
        if normalized in stopwords:
            continue
        candidates = [normalized, *_parts(normalized, config)]
        if config.case_policy == "NORMALIZED_AND_EXACT" and surface != normalized:
            # A case-exact identifier is a rarer signal than its casefolded form, so keeping it
            # as its own term lets IDF reward an exact match without any hand-tuned boost.
            candidates.append(f"{EXACT_PREFIX}{surface}")
        for term in candidates:
            if config.min_term_length <= len(term) <= config.max_term_length:
                produced.append(term)
                if len(produced) >= config.max_terms_per_chunk:
                    return produced
    return produced


def frequencies(text: str, config: SparseAnalyzerConfig) -> tuple[dict[str, int], int]:
    """Term frequencies and the document length used for BM25 length normalization.

    Length counts the emitted term stream rather than the surface words, because that is the same
    unit the term frequencies are counted in; mixing the two would bias the normalization.
    """
    stream = terms(text, config)
    return dict(Counter(stream)), len(stream)


def query_terms(text: str, config: SparseAnalyzerConfig) -> tuple[str, ...]:
    """Distinct query terms, in first-appearance order.

    Repetition in a question is not evidence of importance, so a term contributes once. This is
    the conventional BM25 treatment of query term frequency and it keeps the score dependent on
    the document, which is what the ranking is about.
    """
    seen: list[str] = []
    for term in terms(text, config):
        if term not in seen:
            seen.append(term)
    return tuple(seen)
