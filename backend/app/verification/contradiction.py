"""Claim-level contradiction checks over the whole retained EvidenceSet.

M7 could only see disagreement between evidence blocks, and its own report records that it misses
prose-level contradictions. M8 adds the case that matters most once a draft exists: a claim
supported by the source it cites and contradicted by another source that retrieval kept but the
generator did not cite. That is the shape a generator produces when it picks the passage agreeing
with it.

Rank never breaks these ties. The block a CrossEncoder put first is not thereby true, and source
authority orders which disagreement is reportable — never which source wins.
"""

from collections.abc import Sequence
from uuid import UUID

from app.core.verification_config import ContradictionConfig
from app.evidence.model import EvidenceBlock
from app.retrieval.sparse.analyzer import terms
from app.sufficiency.model import ASSESSMENT_SOURCE_TYPES, REFERENCE_AUTHORITY
from app.verification.claims import ANALYZER, CONNECTIVES
from app.verification.deterministic import measurements
from app.verification.model import Claim, ContradictionFinding


def _assessment(block: EvidenceBlock) -> bool:
    return block.source_type in ASSESSMENT_SOURCE_TYPES or block.authority_level == "ASSESSMENT"


def _about(claim: Claim, block: EvidenceBlock) -> bool:
    """Whether a block discusses the claim's subject closely enough to disagree with it."""
    words = set(terms(claim.text, ANALYZER)) - CONNECTIVES
    if not words:
        return False
    return len(words & set(terms(block.text, ANALYZER))) / len(words) >= 0.5


def detect(
    claims: Sequence[Claim],
    blocks: Sequence[EvidenceBlock],
    config: ContradictionConfig,
) -> list[ContradictionFinding]:
    findings: list[ContradictionFinding] = []
    supplied = {b.evidence_id: b for b in blocks}
    for claim in claims:
        if not claim.material or not claim.cited_evidence_ids:
            continue
        cited_ids = set(claim.cited_evidence_ids)
        cited = [supplied[i] for i in cited_ids if i in supplied]
        retained = [b for b in blocks if b.evidence_id not in cited_ids]
        if config.check_uncited_retained_evidence:
            findings.extend(_uncited_disagreement(claim, cited, retained))
        if config.check_authoritative_disagreement:
            findings.extend(_authority_disagreement(claim, cited, retained))
    return _deduplicate(findings)


def _uncited_disagreement(
    claim: Claim, cited: Sequence[EvidenceBlock], retained: Sequence[EvidenceBlock]
) -> list[ContradictionFinding]:
    """A retained source stating a different value for a measurement the claim asserts."""
    stated = measurements(claim.text)
    if not stated:
        return []
    units = {unit for _, unit in stated if unit}
    out = []
    for block in retained:
        if not _about(claim, block):
            continue
        other = {(v, u) for v, u in measurements(block.text) if u in units}
        conflicting = {pair for pair in other if pair not in stated}
        if not conflicting:
            continue
        out.append(
            ContradictionFinding(
                kind="CONTRADICTED_BY_RETAINED_EVIDENCE",
                description=(
                    "Retrieved evidence that the draft did not cite states a different value for a "
                    "measurement this claim asserts."
                ),
                claim_ids=[claim.claim_id],
                evidence_ids=sorted({block.evidence_id, *(b.evidence_id for b in cited)}, key=str),
                document_version_ids=sorted(
                    {block.document_version_id, *(b.document_version_id for b in cited)}, key=str
                ),
                detail={
                    "claim_states": sorted(f"{v} {u}".strip() for v, u in stated),
                    "retained_source_states": sorted(f"{v} {u}".strip() for v, u in conflicting),
                },
            )
        )
    return out


def _authority_disagreement(
    claim: Claim, cited: Sequence[EvidenceBlock], retained: Sequence[EvidenceBlock]
) -> list[ContradictionFinding]:
    """Assessment-only support while a reference source discusses the same subject differently."""
    if not cited or not all(_assessment(b) for b in cited):
        return []
    stated = measurements(claim.text)
    references = [
        b
        for b in retained
        if not _assessment(b) and b.authority_level in REFERENCE_AUTHORITY and _about(claim, b)
    ]
    out = []
    for block in references:
        other = measurements(block.text)
        if stated and not (stated & other) and other:
            out.append(
                ContradictionFinding(
                    kind="ASSESSMENT_CONTRADICTS_REFERENCE",
                    description=(
                        "This claim rests only on assessment material while a reference source in "
                        "the same evidence set states something different. An examiner's key is "
                        "not automatically medical fact."
                    ),
                    claim_ids=[claim.claim_id],
                    evidence_ids=sorted(
                        {block.evidence_id, *(b.evidence_id for b in cited)}, key=str
                    ),
                    document_version_ids=sorted(
                        {block.document_version_id, *(b.document_version_id for b in cited)},
                        key=str,
                    ),
                    detail={"reference_source": block.document_title},
                )
            )
    return out


def authoritative_disagreement(
    blocks: Sequence[EvidenceBlock],
) -> list[ContradictionFinding]:
    """Two reference-grade sources stating different values for the same labelled measurement.

    Reported whether or not any claim relies on it: an answer built from a corpus that disagrees
    with itself is not something to release, and which source is right is not ours to decide.
    """
    references = [
        b for b in blocks if not _assessment(b) and b.authority_level in REFERENCE_AUTHORITY
    ]
    by_unit: dict[str, dict[str, list[EvidenceBlock]]] = {}
    for block in references:
        for value, unit in measurements(block.text):
            if not unit:
                continue
            by_unit.setdefault(unit, {}).setdefault(value, []).append(block)
    out = []
    for unit, values in by_unit.items():
        if len(values) < 2:
            continue
        versions: set[UUID] = {b.document_version_id for group in values.values() for b in group}
        if len(versions) < 2:
            # One document stating several values for one unit is a range, not a disagreement.
            continue
        involved = [b for group in values.values() for b in group]
        out.append(
            ContradictionFinding(
                kind="AUTHORITATIVE_SOURCES_DISAGREE",
                description=(
                    "Independent reference sources state different values in the same unit. The "
                    "disagreement is preserved rather than resolved."
                ),
                evidence_ids=sorted({b.evidence_id for b in involved}, key=str),
                document_version_ids=sorted(versions, key=str),
                detail={"unit": unit, "values": sorted(values)},
            )
        )
    return out


def _deduplicate(findings: list[ContradictionFinding]) -> list[ContradictionFinding]:
    seen: dict[tuple[str, tuple[str, ...]], ContradictionFinding] = {}
    for finding in findings:
        key = (finding.kind, tuple(sorted(map(str, finding.evidence_ids))))
        if key not in seen:
            seen[key] = finding
    return list(seen.values())
