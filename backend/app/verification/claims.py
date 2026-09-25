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

#: A citation marker a generator wrote into the prose — a bracketed evidence_id or an index like
#: "[2]". It is a pointer to a source, not a statement about a patient, so it carries no claim.
#: Measured need: a repaired draft ended "... [1e5fdd99-6e8b-5c24-93b6-5c6202ae8c66]", the sentence
#: splitter made the marker its own sentence, its hex groups counted as content terms, and the
#: marker was verified as a material medical claim — which no citation of its own could support.
CITATION_MARKER = re.compile(r"\[\s*(?:[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|\d{1,3})\s*\]")

#: A coordination sitting inside one of these joins predicates *within* a clause, not two clauses.
RELATIVE_PRONOUNS = ("that", "which", "who", "whom", "whose")

#: "respectively" binds two coordinated lists to each other, so neither list is a proposition on
#: its own. Measured: "Midline anterior and posterior cerebellar incisurae accommodate the
#: brainstem and falx cerebelli, respectively" was split at the second coordinator, and the half
#: that survived — "...incisurae accommodate the brainstem" — says both incisurae hold the
#: brainstem. The source says the falx holds the posterior one. The verifier correctly called that
#: CONTRADICTED, but the draft never said it; the split did.
DISTRIBUTIVE = re.compile(r"\brespectively\b", re.IGNORECASE)

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


def _propositions(
    text: str,
    offset: int,
    config: ClaimExtractionConfig,
    declared: list[tuple[set[str], list[UUID]]],
) -> list[tuple[int, int]]:
    if not config.split_coordinated_clauses or DISTRIBUTIVE.search(text):
        return [(offset, offset + len(text))]
    parts, start = [], 0
    for match in COORDINATORS.finditer(text):
        left, right = text[start : match.start()], text[match.end() :]
        # Split only when both halves stand alone; "5 mg and 10 mg" must stay one proposition.
        both = min(_standalone(left), _standalone(right))
        if (
            both >= config.min_material_terms
            and not _subordinate(left)
            # Splitting may not manufacture an uncited claim. Each half has to be a proposition the
            # generator itself declared and bound to evidence; a half that matches no declaration is
            # not a clause this sentence contains, it is a fragment this function invented, and
            # failing it for having no citation would blame the generator for our own cut.
            #
            # Undeclared text is not thereby excused. It stays attached to the sentence it was
            # written in and is verified as part of it, so "Drug A treats B and is safe in
            # pregnancy" — declared only as far as "treats B" — is checked whole, and the verifier's
            # first rule refuses a statement whose evidence establishes only one of its parts.
            and _citations(left, declared)
            and _citations(right, declared)
        ):
            parts.append((offset + start, offset + match.start()))
            start = match.end()
    parts.append((offset + start, offset + len(text)))
    return parts


def _subordinate(left: str) -> bool:
    """Is the coordinator that follows `left` inside an unclosed relative clause?

    "the cerebellar surface **that** faces and conforms to the tentorium" coordinates two verbs of
    one relative clause, and splitting it produced "The tentorial surface is the cerebellar surface
    that faces" — a claim that asserts nothing a verifier can check. A relative clause that has
    already closed with a comma ("Drug A, which is a beta blocker, treats X and reduces Y") is not
    this case, and that coordination still splits.

    Not splitting is never the lenient direction on its own: the conjunction is then verified whole,
    and the verifier's first rule is that a statement joining two propositions is not SUPPORTED
    when the evidence establishes only one.
    """
    lowered = left.casefold()
    positions = [lowered.rfind(f" {word} ") for word in RELATIVE_PRONOUNS]
    last = max(positions)
    return last >= 0 and "," not in left[last:]


def _content(text: str) -> int:
    return len([t for t in terms(CITATION_MARKER.sub(" ", text), ANALYZER) if t not in CONNECTIVES])


def _standalone(text: str) -> int:
    """Distinct meaningful words, as a reader would count them.

    `_content` counts the *index* term stream, which deliberately emits a word more than once: the
    analyzer adds a case-exact duplicate (`Midline` -> `midline`, `^Midline`) so that BM25 can
    reward an exact identifier match, and it adds the parts of a compound. That is right for
    scoring and wrong for deciding whether half of a sentence stands on its own — it made the
    two-word fragment "Midline anterior" count three terms and pass for an independent clause, so
    "Midline anterior and posterior cerebellar incisurae accommodate the brainstem" was split
    across its subject. The resulting fragment matched no declared claim, so it carried no
    citation, and one mutilated noun phrase abstained an answer whose evidence was on the page.
    """
    words = {
        word.casefold()
        for word in re.findall(r"[^\W\d_]+", CITATION_MARKER.sub(" ", text), re.UNICODE)
    }
    return len(words - CONNECTIVES)


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
    spans = _sentences(draft.answer)
    for index, (start, end) in enumerate(spans):
        sentence = draft.answer[start:end]
        # One sentence back, because a draft's second sentence routinely opens with "It" or "Its"
        # and the antecedent is in the first. A verifier given only the sentence answered exactly
        # that way — "the pronoun 'It' has no identifiable antecedent" — and refused a claim the
        # draft had stated plainly. Bounded at one sentence: this resolves reference, and a
        # verifier told the context is not evidence has less draft text to mistake for source.
        previous = draft.answer[spans[index - 1][0] : spans[index - 1][1]] if index else ""
        context = (previous + " " + sentence).strip() if previous else sentence.strip()
        for begin, finish in _propositions(sentence, start, config, declared):
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
                    context=context,
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
    words = set(terms(CITATION_MARKER.sub(" ", text), ANALYZER)) - CONNECTIVES
    if not words:
        return []
    found: list[UUID] = []
    for declared_terms, evidence_ids in declared:
        if not declared_terms:
            continue
        shared = len(words & declared_terms)
        # Either direction attributes. A proposition mostly made of a declared claim's words is
        # that claim; so is a sentence that merges two declared claims and is therefore mostly
        # *not* any single one of them — "Midline anterior and posterior cerebellar incisurae
        # accommodate the brainstem and falx cerebelli, respectively" covers two declarations at
        # 0.45 each from its own side and was attributed to neither, so a generator that had bound
        # every word of it to evidence was reported as having cited nothing.
        #
        # Reading a declaration into a longer sentence excuses nothing that sentence also says:
        # the verifier judges the whole statement against exactly these citations, so any extra
        # assertion riding along has to be supported too or the claim fails.
        if shared / len(words) >= 0.5 or shared / len(declared_terms) >= 0.5:
            found.extend(v for v in evidence_ids if v not in found)
    return found
