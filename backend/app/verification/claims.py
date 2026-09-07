"""Deterministic decomposition of a draft answer into atomic verifiable claims.

Extraction runs over `draft.answer` — the text a reader would actually see — rather than over the
claim list the generator chose to declare. A generator that writes a sentence and simply omits it
from its own claim list would otherwise ship an unverified medical statement inside a "verified"
answer. Declared claims are used only to supply citations to the propositions they cover.

Splitting is biased toward over-splitting. An extra fragment costs one more verification; a
proposition left welded to a supported one is how "Drug A treats B **and is safe in pregnancy**"
passes on the strength of its first half.
"""

import re
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.retrieval_config import SparseAnalyzerConfig
from app.core.verification_config import ClaimExtractionConfig
from app.evidence.model import EvidenceBlock
from app.generation.grounding.model import GroundedDraft
from app.retrieval.sparse.analyzer import terms
from app.verification.model import Claim, ClaimType

# Fixed tokenization, deliberately not the tenant's active index analyzer: rebuilding a lexical
# index must not change what a claim decomposes into.
ANALYZER = SparseAnalyzerConfig()

# Sentence end: terminator, then whitespace, then something that starts a sentence. The lookbehind
# keeps decimals ("7.5 mg"), and the abbreviation guard keeps "e.g." and "Fig." intact.
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
ABBREVIATIONS = frozenset({"e.g", "i.e", "fig", "no", "vs", "approx", "cf", "etc", "dr", "mg"})

# Coordinated clauses are split only on these, and only when both halves carry a proposition.
COORDINATORS = re.compile(r"\s+(?:and|but|whereas|while|although)\s+|\s*;\s*", re.IGNORECASE)

CONNECTIVES = frozenset(
    {
        "therefore",
        "additionally",
        "furthermore",
        "moreover",
        "however",
        "thus",
        "hence",
        "based",
        "evidence",
        "according",
        "source",
        "sources",
        "states",
        "state",
        "stated",
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "to",
        "is",
        "are",
        "was",
        "were",
        "it",
        "this",
        "that",
        "these",
        "those",
        "as",
        "for",
        "with",
        "by",
        "from",
        "at",
        "be",
        "been",
    }
)

NEGATIONS = frozenset(
    {"not", "no", "never", "without", "neither", "nor", "cannot", "contraindicated"}
)
QUALIFIERS = frozenset(
    {
        "may",
        "might",
        "can",
        "could",
        "usually",
        "often",
        "rarely",
        "sometimes",
        "generally",
        "typically",
        "occasionally",
        "possibly",
        "probably",
        "suggests",
        "associated",
    }
)
NUMBER = re.compile(r"\d")

TABLE_CHUNKS = frozenset({"TABLE", "TABLE_PART"})
FORMULA_CHUNKS = frozenset({"FORMULA"})
FIGURE_CHUNKS = frozenset({"FIGURE_CONTEXT"})
ASSESSMENT_CHUNKS = frozenset({"QUESTION", "QUESTION_EXPLANATION"})


def _sentences(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for match in SENTENCE.finditer(text):
        candidate = text[start : match.start()]
        tail = candidate.rstrip(".").rsplit(" ", 1)[-1].lower()
        if tail in ABBREVIATIONS:
            continue
        spans.append((start, match.start()))
        start = match.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _propositions(text: str, offset: int, config: ClaimExtractionConfig) -> list[tuple[int, int]]:
    if not config.split_coordinated_clauses:
        return [(offset, offset + len(text))]
    parts, start = [], 0
    for match in COORDINATORS.finditer(text):
        left, right = text[start : match.start()], text[match.end() :]
        # Split only when both halves stand alone; "5 mg and 10 mg" must stay one proposition.
        both = min(_content(left), _content(right))
        if both >= config.min_material_terms:
            parts.append((offset + start, offset + match.start()))
            start = match.end()
    parts.append((offset + start, offset + len(text)))
    return parts


def _content(text: str) -> int:
    return len([t for t in terms(text, ANALYZER) if t not in CONNECTIVES])


def classify(text: str, cited: list[EvidenceBlock], material: bool) -> ClaimType:
    """What kind of proposition this is, which decides which extra checks it must survive."""
    if not material:
        return "NON_MATERIAL"
    words = set(terms(text, ANALYZER))
    kinds = {block.chunk_type for block in cited}
    # A claim resting on a figure needs the figure read, and nothing in this system reads one.
    if kinds & FIGURE_CHUNKS or any(b.requires_visual_evidence for b in cited):
        return "VISUAL_DEPENDENT"
    if NUMBER.search(text):
        return "NUMERIC"
    if words & NEGATIONS:
        return "NEGATED"
    if kinds & TABLE_CHUNKS:
        return "TABLE_DERIVED"
    if kinds & FORMULA_CHUNKS:
        return "FORMULA_DERIVED"
    if kinds & ASSESSMENT_CHUNKS:
        return "ASSESSMENT_DERIVED"
    if words & QUALIFIERS:
        return "QUALIFIED"
    return "FACTUAL"


def extract(
    draft: GroundedDraft, blocks: dict[str, EvidenceBlock], config: ClaimExtractionConfig
) -> list[Claim]:
    declared = [
        (set(terms(claim.text, ANALYZER)) - CONNECTIVES, list(claim.evidence_ids))
        for claim in draft.claims
    ]
    claims: list[Claim] = []
    for index, (start, end) in enumerate(_sentences(draft.answer)):
        sentence = draft.answer[start:end]
        for begin, finish in _propositions(sentence, start, config):
            text = draft.answer[begin:finish].strip()
            if not text:
                continue
            material = _content(text) >= config.min_material_terms
            cited = _citations(text, declared) if material else []
            resolved = [blocks[str(v)] for v in cited if str(v) in blocks]
            claims.append(
                Claim(
                    claim_id=uuid5(NAMESPACE_URL, f"m8:{draft.draft_id}:{index}:{begin}:{text}"),
                    text=text,
                    start=begin,
                    end=begin + len(text),
                    claim_type=classify(text, resolved, material),
                    material=material,
                    cited_evidence_ids=cited,
                )
            )
            if len(claims) >= config.max_claims:
                return claims
    return claims


def _citations(text: str, declared: list[tuple[set[str], list[UUID]]]) -> list[UUID]:
    """Citations from every declared claim that overlaps this proposition.

    A proposition matching no declared claim keeps an empty list, which the verifier treats as a
    failure rather than as an absence of opinion — that is the whole point of extracting from the
    answer text instead of from the declarations.
    """
    words = set(terms(text, ANALYZER)) - CONNECTIVES
    if not words:
        return []
    found: list[UUID] = []
    for declared_terms, evidence_ids in declared:
        if not declared_terms:
            continue
        overlap = len(words & declared_terms) / len(words)
        if overlap >= 0.5:
            found.extend(v for v in evidence_ids if v not in found)
    return found
