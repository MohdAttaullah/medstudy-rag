"""Runs the checks in the order that makes them safe, then applies the final policy.

Order matters. Deterministic checks run first and bind: a claim that cites a nonexistent block, a
wrong dose or a reversed negation is failed before a model is asked anything, so no fluent verdict
can talk the system past a fact it could have simply looked up. The semantic verifier is only ever
able to fail a claim that survived that layer.
"""

from collections.abc import Sequence
from time import perf_counter
from uuid import UUID

from app.core.verification_config import (
    ClaimVerificationConfig,
    ContradictionConfig,
    FinalVerificationConfig,
)
from app.evidence.model import EvidenceBlock
from app.verification import contradiction as contradiction_checks
from app.verification import deterministic as checks
from app.verification.model import (
    REPAIRABLE,
    Claim,
    ClaimVerification,
    ContradictionFinding,
    Outcome,
    ReasonCode,
    Verdict,
    VerificationError,
)
from app.verification.verifier import ClaimVerifier, VerifiableClaim

# How a deterministic failure is reported as a verdict. A wrong number is not a matter of degree.
DETERMINISTIC_VERDICT: dict[str, Verdict] = {
    "CLAIM_NOT_CITED": "UNSUPPORTED",
    "UNKNOWN_CITATION": "UNVERIFIABLE",
    "PROVENANCE_UNRESOLVED": "UNVERIFIABLE",
    "TENANT_SCOPE_VIOLATION": "UNVERIFIABLE",
    "NUMERIC_MISMATCH": "CONTRADICTED",
    "UNIT_MISMATCH": "CONTRADICTED",
    "NEGATION_REVERSED": "CONTRADICTED",
    "OVERSTATED_CERTAINTY": "UNSUPPORTED",
    "VISUAL_INTERPRETATION_REQUIRED": "UNVERIFIABLE",
}

SEMANTIC_REASON: dict[Verdict, ReasonCode] = {
    "UNSUPPORTED": "SEMANTICALLY_UNSUPPORTED",
    "CONTRADICTED": "SEMANTICALLY_CONTRADICTED",
    "INSUFFICIENT_EVIDENCE": "SEMANTIC_EVIDENCE_INSUFFICIENT",
    "UNVERIFIABLE": "SEMANTIC_EVIDENCE_INSUFFICIENT",
}


def deterministic_pass(
    claim: Claim,
    supplied: dict[str, EvidenceBlock],
    tenant_id: UUID,
    config: ClaimVerificationConfig,
) -> tuple[list[ReasonCode], list[EvidenceBlock]]:
    """Every check that can be decided by looking. Returns its findings and the cited blocks."""
    if not claim.material:
        return [], []
    codes = checks.check_citations(claim, supplied, tenant_id, config)
    cited = [supplied[str(v)] for v in claim.cited_evidence_ids if str(v) in supplied]
    codes.extend(checks.check_structured_evidence(claim, cited))
    if cited:
        if config.check_numeric_agreement:
            codes.extend(checks.check_numeric(claim, cited))
        if config.check_negation_agreement:
            codes.extend(checks.check_negation(claim, cited))
        if config.check_certainty_overstatement:
            codes.extend(checks.check_certainty(claim, cited))
    return list(dict.fromkeys(codes)), cited


async def verify_claims(
    claims: Sequence[Claim],
    blocks: Sequence[EvidenceBlock],
    tenant_id: UUID,
    verifier: ClaimVerifier | None,
    config: ClaimVerificationConfig,
) -> list[ClaimVerification]:
    supplied = {str(b.evidence_id): b for b in blocks}
    results: list[ClaimVerification] = []
    for claim in claims:
        if not claim.material:
            results.append(
                ClaimVerification(
                    claim_id=claim.claim_id,
                    claim_text=claim.text,
                    claim_type=claim.claim_type,
                    material=False,
                    verdict="SUPPORTED",
                    reason_codes=["NON_MATERIAL_CLAIM"],
                )
            )
            continue

        codes, cited = deterministic_pass(claim, supplied, tenant_id, config)
        if codes:
            # Final. The model is not consulted about a claim that already failed on the facts.
            results.append(
                ClaimVerification(
                    claim_id=claim.claim_id,
                    claim_text=claim.text,
                    claim_type=claim.claim_type,
                    material=True,
                    verdict=DETERMINISTIC_VERDICT[codes[0]],
                    reason_codes=codes,
                    supporting_evidence_ids=[],
                    contradicting_evidence_ids=list(claim.cited_evidence_ids),
                    detail={"stage": "deterministic"},
                )
            )
            continue

        if not config.semantic_verification_enabled:
            results.append(
                ClaimVerification(
                    claim_id=claim.claim_id,
                    claim_text=claim.text,
                    claim_type=claim.claim_type,
                    material=True,
                    verdict="SUPPORTED",
                    reason_codes=["SUPPORTED_BY_CITED_EVIDENCE"],
                    supporting_evidence_ids=list(claim.cited_evidence_ids),
                    detail={"stage": "deterministic-only"},
                )
            )
            continue

        if verifier is None:
            # Fail closed: no verifier means nothing was verified, which is not the same as passing.
            raise VerificationError("VERIFIER_FAILED", "No claim verifier is configured.")

        verdict = await verifier.verify(
            VerifiableClaim(
                claim_id=claim.claim_id,
                text=claim.text,
                evidence=cited,
                context=claim.context,
            )
        )
        supported = verdict.verdict == "SUPPORTED"
        results.append(
            ClaimVerification(
                claim_id=claim.claim_id,
                claim_text=claim.text,
                claim_type=claim.claim_type,
                material=True,
                verdict=verdict.verdict,
                reason_codes=(
                    ["SUPPORTED_BY_CITED_EVIDENCE"]
                    if supported
                    else [SEMANTIC_REASON[verdict.verdict]]
                ),
                supporting_evidence_ids=(
                    verdict.supporting_evidence_ids or list(claim.cited_evidence_ids)
                    if supported
                    else []
                ),
                contradicting_evidence_ids=list(verdict.contradicting_evidence_ids),
                detail={"stage": "semantic", "rationale": verdict.rationale[:500]},
            )
        )
    return results


def find_contradictions(
    claims: Sequence[Claim], blocks: Sequence[EvidenceBlock], config: ContradictionConfig
) -> list[ContradictionFinding]:
    findings = list(contradiction_checks.detect(claims, blocks, config))
    if config.check_authoritative_disagreement:
        findings.extend(contradiction_checks.authoritative_disagreement(blocks))
    return contradiction_checks._deduplicate(findings)


def decide(
    verifications: Sequence[ClaimVerification],
    contradictions: Sequence[ContradictionFinding],
    repair_count: int,
    repair_allowed: bool,
    config: FinalVerificationConfig,
) -> tuple[Outcome, list[ReasonCode]]:
    """PASS only when every material claim survived and nothing in the evidence disagrees."""
    failed = [v for v in verifications if v.failed]
    codes: list[ReasonCode] = []
    for verification in failed:
        codes.extend(verification.reason_codes)
    for finding in contradictions:
        codes.append(finding.kind)
    codes = list(dict.fromkeys(codes))

    if contradictions and config.contradiction_abstains:
        # The corpus disagrees with itself. Rewriting the answer cannot settle that, and choosing a
        # source would be exactly the silent resolution the architecture forbids.
        return "ABSTAIN", codes
    if not failed:
        return "PASS", codes
    # One repair is offered only when every failure is something a rewrite could remove.
    if repair_allowed and repair_count == 0 and all(code in REPAIRABLE for code in codes):
        return "REGENERATE_ONCE", codes
    return "ABSTAIN", codes


def timed(started: float) -> float:
    return (perf_counter() - started) * 1000
