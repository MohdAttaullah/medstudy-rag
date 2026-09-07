"""Evidence-level conflict detection.

Deliberately narrow. M7 asks one question: does the EvidenceSet already contain a disagreement that
makes ordinary generation unsafe? It does **not** analyse claims in a generated draft — that is M8,
and mixing the two would put a claim verifier in front of the thing it is supposed to verify.

Two detectors, both deterministic:

1. An assessment key (a question bank's or answer key's recorded answer) whose content the
   reference corpus in the same EvidenceSet does not support.
2. Two independent non-assessment sources stating different values for the same labelled quantity.

Both over-trigger rather than under-trigger. In this architecture an unnecessary abstention is a
far cheaper mistake than an unsupported medical answer, so a false conflict is an acceptable cost
and a missed one is not. The known false-positive shapes are recorded in the M7 report.
"""

import re
from collections import defaultdict
from typing import Any

from app.evidence.model import EvidenceBlock
from app.retrieval.sparse.analyzer import terms
from app.sufficiency.model import (
    ASSESSMENT_SOURCE_TYPES,
    REFERENCE_AUTHORITY,
    EvidenceConflict,
)
from app.sufficiency.question import ANALYZER

# A number with an optional unit, preceded by up to four words that name it. The label is what
# makes this a comparison of like with like: "dose 5 mg" and "clearance 5 mg" are not a conflict.
MEASUREMENT = re.compile(
    r"(?P<label>(?:[A-Za-z][A-Za-z0-9\-+/']*[ \t]+){0,4})"
    r"(?P<value>\d+(?:\.\d+)?)[ \t]*"
    r"(?P<unit>%|[A-Za-z][A-Za-z0-9µ/%\-]*)?",
)

# Words that carry no identity for a quantity; dropping them lets "the mean dose" and "mean dose"
# describe the same thing.
LABEL_NOISE = frozenset({"the", "a", "an", "of", "is", "was", "are", "were", "at", "to", "and"})


def _label(raw: str) -> tuple[str, ...]:
    return tuple(t for t in terms(raw, ANALYZER) if t not in LABEL_NOISE)


def measurements(text: str) -> dict[tuple[tuple[str, ...], str], set[str]]:
    """Map (label, unit) to the distinct literal values stated for it."""
    found: dict[tuple[tuple[str, ...], str], set[str]] = defaultdict(set)
    for match in MEASUREMENT.finditer(text):
        label = _label(match.group("label") or "")
        if not label:
            continue
        unit = (match.group("unit") or "").lower()
        found[(label, unit)].add(match.group("value").rstrip("0").rstrip(".") or "0")
    return found


def _assessment(block: EvidenceBlock) -> bool:
    return block.source_type in ASSESSMENT_SOURCE_TYPES or block.authority_level == "ASSESSMENT"


def detect(blocks: list[EvidenceBlock]) -> list[EvidenceConflict]:
    conflicts: list[EvidenceConflict] = []
    reference = [
        b for b in blocks if not _assessment(b) and b.authority_level in REFERENCE_AUTHORITY
    ]
    conflicts.extend(_assessment_unsupported(blocks, reference))
    conflicts.extend(_value_conflicts([b for b in blocks if not _assessment(b)]))
    return conflicts


def _assessment_unsupported(
    blocks: list[EvidenceBlock], reference: list[EvidenceBlock]
) -> list[EvidenceConflict]:
    """An examiner's key that the reference evidence does not carry is an unresolved disagreement.

    Only raised when reference evidence is actually present: with no reference source there is
    nothing to disagree with, and that case is already insufficiency, not conflict.
    """
    if not reference:
        return []
    supported = set()
    for block in reference:
        supported |= set(terms(block.text, ANALYZER))
    out = []
    for block in blocks:
        answer = (block.question or {}).get("explicit_answer")
        if not _assessment(block) or not answer:
            continue
        claimed = set(terms(str(answer), ANALYZER))
        if claimed and not claimed <= supported:
            out.append(
                EvidenceConflict(
                    kind="ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE",
                    description=(
                        "An assessment source states an answer that the reference evidence in this "
                        "set does not support. Assessment keys are not automatically authoritative."
                    ),
                    evidence_ids=[block.evidence_id, *(b.evidence_id for b in reference)],
                    document_version_ids=sorted(
                        {block.document_version_id, *(b.document_version_id for b in reference)},
                        key=str,
                    ),
                    detail={"unsupported_terms": sorted(claimed - supported)},
                )
            )
    return out


def _value_conflicts(blocks: list[EvidenceBlock]) -> list[EvidenceConflict]:
    """Different values for the same labelled quantity, stated by different document versions."""
    stated: dict[tuple[tuple[str, ...], str], dict[str, list[EvidenceBlock]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for block in blocks:
        for key, values in measurements(block.text).items():
            for value in values:
                stated[key][value].append(block)
    out = []
    for (label, unit), by_value in stated.items():
        if len(by_value) < 2:
            continue
        versions = {b.document_version_id for blocks_ in by_value.values() for b in blocks_}
        if len(versions) < 2:
            # One document stating several values for one label is a range or a list, not a
            # disagreement between sources.
            continue
        involved = [b for blocks_ in by_value.values() for b in blocks_]
        detail: dict[str, Any] = {
            "label": " ".join(label),
            "unit": unit,
            "values": sorted(by_value),
        }
        out.append(
            EvidenceConflict(
                kind="INDEPENDENT_SOURCE_VALUE_CONFLICT",
                description=(
                    "Independent sources state different values for the same labelled quantity."
                ),
                evidence_ids=sorted({b.evidence_id for b in involved}, key=str),
                document_version_ids=sorted(versions, key=str),
                detail=detail,
            )
        )
    return out
