"""Checks that run before any model is consulted, and that no model may overrule.

Whether a citation exists, whether its provenance resolves, whether a dose matches and whether a
negation survived are all decidable by looking. Asking a language model to adjudicate any of them
would trade a certain answer for an uncertain one, and would let a fluent verifier wave through the
two failure modes that hurt most in a medical answer: a wrong number and a reversed negation.
"""

import re
from collections.abc import Sequence
from uuid import UUID

from app.core.verification_config import ClaimVerificationConfig
from app.evidence.model import EvidenceBlock
from app.retrieval.sparse.analyzer import terms
from app.verification.claims import (
    ANALYZER,
    COORDINATORS,
    FIGURE_CHUNKS,
    FORMULA_CHUNKS,
    NEGATIONS,
    QUALIFIERS,
    TABLE_CHUNKS,
)
from app.verification.model import Claim, ReasonCode

# A number with the unit that follows it. The unit is part of the identity: 5 mg and 5 mL are not
# the same measurement, and comparing bare numbers would call them equal.
MEASUREMENT = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>%|[A-Za-zµ][A-Za-z0-9µ/·]*(?:/[A-Za-z0-9µ]+)*)?"
)

# Words that assert more than a qualified source supports. A source saying a drug "may be
# associated with" an effect does not support an answer saying it "causes" one.
STRONG = frozenset(
    {
        "causes",
        "cause",
        "always",
        "must",
        "invariably",
        "never",
        "all",
        "every",
        "guarantees",
        "eliminates",
        "prevents",
        "contraindicated",
        "required",
        "requires",
        "definitive",
        "diagnostic",
        "confirms",
    }
)

UNIT_ALIASES = {
    "milligram": "mg",
    "milligrams": "mg",
    "mgs": "mg",
    "microgram": "mcg",
    "micrograms": "mcg",
    "µg": "mcg",
    "ug": "mcg",
    "gram": "g",
    "grams": "g",
    "millilitre": "ml",
    "millilitres": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "litre": "l",
    "litres": "l",
    "liter": "l",
    "liters": "l",
    "hour": "h",
    "hours": "h",
    "hr": "h",
    "hrs": "h",
    "minute": "min",
    "minutes": "min",
    "mins": "min",
    "day": "d",
    "days": "d",
    "percent": "%",
    "percentage": "%",
    "pct": "%",
    "kilogram": "kg",
    "kilograms": "kg",
}


def normalize_unit(unit: str | None) -> str:
    if not unit:
        return ""
    lowered = unit.strip().lower().rstrip(".")
    return UNIT_ALIASES.get(lowered, lowered)


def normalize_value(value: str) -> str:
    cleaned = value.replace(",", "")
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned or "0"


def measurements(text: str) -> set[tuple[str, str]]:
    """Every (value, normalized unit) pair a text states."""
    found = set()
    for match in MEASUREMENT.finditer(text):
        found.add((normalize_value(match.group("value")), normalize_unit(match.group("unit"))))
    return found


def check_numeric(claim: Claim, cited: Sequence[EvidenceBlock]) -> list[ReasonCode]:
    """Every measurement asserted by the claim must appear in the evidence it cites.

    Both halves matter. A different value is a wrong dose; the same value with a different unit is
    equally wrong and reads just as plausibly.
    """
    stated = measurements(claim.text)
    if not stated:
        return []
    supported: set[tuple[str, str]] = set()
    for block in cited:
        supported |= measurements(block.text)
    codes: list[ReasonCode] = []
    values = {value for value, _ in supported}
    for value, unit in stated:
        if (value, unit) in supported:
            continue
        # Distinguishing the two failures tells a reader whether the number or the unit was wrong.
        codes.append("UNIT_MISMATCH" if value in values else "NUMERIC_MISMATCH")
    return list(dict.fromkeys(codes))


def check_negation(claim: Claim, cited: Sequence[EvidenceBlock]) -> list[ReasonCode]:
    """A claim must not reverse the polarity of the evidence it rests on.

    Reported only on an actual disagreement: some passage about this subject must carry the
    opposite polarity and none may carry the claim's. Firing merely because no single sentence
    cleared the overlap bar would flag every claim drawn from a table or a list, where the subject
    is spread across short structured fragments.
    """
    words = set(terms(claim.text, ANALYZER))
    claim_negated = bool(words & NEGATIONS)
    content = words - NEGATIONS - QUALIFIERS
    if not content:
        return []
    relevant: list[set[str]] = []
    for block in cited:
        for sentence in re.split(r"(?<=[.!?])\s+", block.text):
            # Polarity belongs to a clause, not to the sentence that contains it. The source reads
            # "the transition from the vermis to the hemispheres is smooth and not marked by the
            # deep fissures", and a claim quoting the positive half — "the transition is smooth" —
            # was reported as reversing a negation that belongs to the other half. Each clause is
            # therefore weighed on its own, and the sentence entire is kept as a candidate too, so
            # a negation spanning a coordination ("not A and B") still matches a claim about it.
            for passage in (*COORDINATORS.split(sentence), sentence):
                passage_words = set(terms(passage, ANALYZER))
                if len(content & passage_words) / len(content) >= 0.6:
                    relevant.append(passage_words)
    if not relevant:
        # Structured evidence spreads one subject over several fragments, so fall back to the
        # block as a whole before concluding anything about polarity.
        for block in cited:
            block_words = set(terms(block.text, ANALYZER))
            if len(content & block_words) / len(content) >= 0.6:
                relevant.append(block_words)
    if not relevant:
        return []
    if any(bool(passage & NEGATIONS) == claim_negated for passage in relevant):
        return []
    return ["NEGATION_REVERSED"]


def _discusses(claim: Claim, cited: Sequence[EvidenceBlock]) -> bool:
    words = set(terms(claim.text, ANALYZER)) - NEGATIONS - QUALIFIERS
    if not words:
        return False
    for block in cited:
        block_words = set(terms(block.text, ANALYZER))
        if len(words & block_words) / len(words) >= 0.6:
            return True
    return False


def check_certainty(claim: Claim, cited: Sequence[EvidenceBlock]) -> list[ReasonCode]:
    """A claim may not be more certain than its evidence.

    Only fires when the evidence hedges and the claim does not: a source that already states the
    strong form supports the strong claim.
    """
    claim_words = set(terms(claim.text, ANALYZER))
    asserted = claim_words & STRONG
    if not asserted:
        return []
    for block in cited:
        block_words = set(terms(block.text, ANALYZER))
        if asserted & block_words:
            return []
    hedged = any(set(terms(b.text, ANALYZER)) & QUALIFIERS for b in cited)
    return ["OVERSTATED_CERTAINTY"] if hedged and _discusses(claim, cited) else []


def check_citations(
    claim: Claim,
    supplied: dict[str, EvidenceBlock],
    tenant_id: UUID,
    config: ClaimVerificationConfig,
) -> list[ReasonCode]:
    """Citation identity, ownership and provenance — decided by looking, never by asking."""
    codes: list[ReasonCode] = []
    if not claim.cited_evidence_ids:
        if config.require_citation_for_material_claims:
            codes.append("CLAIM_NOT_CITED")
        return codes
    for evidence_id in claim.cited_evidence_ids:
        block = supplied.get(str(evidence_id))
        if block is None:
            # Existing somewhere is not the test; being in this request's EvidenceSet is.
            codes.append("UNKNOWN_CITATION")
            continue
        if not _provenance_resolves(block):
            codes.append("PROVENANCE_UNRESOLVED")
    return list(dict.fromkeys(codes))


def _provenance_resolves(block: EvidenceBlock) -> bool:
    """Every block must still resolve back to a real place in a real document."""
    if not (block.source_element_ids and block.source_spans and block.pages):
        return False
    if not (block.document_id and block.document_version_id and block.parse_run_id):
        return False
    for span in block.source_spans:
        if span.start < 0 or span.end < span.start:
            return False
        if span.element_id not in block.source_element_ids:
            return False
    for artifact in block.artifacts:
        if not artifact.artifact_id or not artifact.kind:
            return False
    return True


def check_structured_evidence(claim: Claim, cited: Sequence[EvidenceBlock]) -> list[ReasonCode]:
    """Table, formula and figure claims must rest on the canonical artifact, not flattened prose.

    Driven by what the claim actually cites rather than by its single type label. A claim stating a
    number taken from a table is classified NUMERIC, and keying this check off that label alone let
    a table without header rows through — the cells cannot be read correctly without them, and the
    verifier must never reconstruct the missing structure.
    """
    codes: list[ReasonCode] = []
    if any(b.chunk_type in TABLE_CHUNKS for b in cited):
        tables = [a for b in cited for a in b.artifacts if a.kind == "TABLE"]
        if not tables or not all(a.header_rows for a in tables):
            codes.append("PROVENANCE_UNRESOLVED")
    if claim.claim_type == "FORMULA_DERIVED" or any(b.chunk_type in FORMULA_CHUNKS for b in cited):
        if not any(a.kind == "FORMULA" for b in cited for a in b.artifacts):
            codes.append("PROVENANCE_UNRESOLVED")
    if claim.claim_type == "VISUAL_DEPENDENT" or any(
        b.requires_visual_evidence or b.chunk_type in FIGURE_CHUNKS for b in cited
    ):
        # No approved path reads an original figure, and a caption is not the figure.
        codes.append("VISUAL_INTERPRETATION_REQUIRED")
    return list(dict.fromkeys(codes))
