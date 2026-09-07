"""M8 orchestration: verify the M7 draft, repair at most once, then release or abstain.

The whole point of this service is that a draft arrives untrusted and only one path exists out of
that state. `verified=True` is constructed in exactly one place — behind a PASS — and every other
path returns a typed abstention.
"""

import logging
from time import perf_counter
from typing import Any, cast
from uuid import UUID, uuid4

from app.core.config import Settings
from app.core.errors import DomainError
from app.evidence.model import EvidenceSet
from app.generation.citations.validate import bind
from app.generation.errors import GenerationError
from app.generation.grounding.model import GroundedDraft, ProviderDraft
from app.generation.prompts.grounded import render_evidence
from app.generation.prompts.repair import REPAIR_POLICY, repair_message
from app.generation.providers.base import LLMProvider
from app.retrieval.model import RetrievalFilters
from app.security.auth import Principal
from app.services.generation import GenerationService
from app.verification import claims as extraction
from app.verification.engine import decide, find_contradictions, verify_claims
from app.verification.model import (
    AbstentionReason,
    Claim,
    ClaimVerification,
    ContradictionFinding,
    Outcome,
    ReasonCode,
    VerificationAbstention,
    VerificationError,
    VerificationReport,
    VerifiedAnswer,
)
from app.verification.verifier import ClaimVerifier, build_verifier

ABSTENTION_REASON: dict[str, "AbstentionReason"] = {
    "CLAIM_NOT_CITED": "UNSUPPORTED_CLAIM",
    "SEMANTICALLY_UNSUPPORTED": "UNSUPPORTED_CLAIM",
    "SEMANTIC_EVIDENCE_INSUFFICIENT": "UNSUPPORTED_CLAIM",
    "OVERSTATED_CERTAINTY": "UNSUPPORTED_CLAIM",
    "NUMERIC_MISMATCH": "CONTRADICTED_CLAIM",
    "UNIT_MISMATCH": "CONTRADICTED_CLAIM",
    "NEGATION_REVERSED": "CONTRADICTED_CLAIM",
    "SEMANTICALLY_CONTRADICTED": "CONTRADICTED_CLAIM",
    "CONTRADICTED_BY_RETAINED_EVIDENCE": "EVIDENCE_CONFLICT",
    "AUTHORITATIVE_SOURCES_DISAGREE": "EVIDENCE_CONFLICT",
    "ASSESSMENT_CONTRADICTS_REFERENCE": "EVIDENCE_CONFLICT",
    "UNKNOWN_CITATION": "CITATION_INVALID",
    "PROVENANCE_UNRESOLVED": "CITATION_INVALID",
    "TENANT_SCOPE_VIOLATION": "CITATION_INVALID",
    "VISUAL_INTERPRETATION_REQUIRED": "VERIFICATION_UNAVAILABLE",
    "VERIFIER_FAILED": "VERIFICATION_UNAVAILABLE",
    "VERIFIER_MALFORMED_OUTPUT": "VERIFICATION_UNAVAILABLE",
    "VERIFIER_UNKNOWN_EVIDENCE": "VERIFICATION_UNAVAILABLE",
}

# A generation failure during the single repair is still a repair that failed, so it abstains
# rather than surfacing as an error — but under the reason that actually caused it.
GENERATION_FAILURE = {
    "GENERATION_UNKNOWN_CITATION": "UNKNOWN_CITATION",
    "GENERATION_MISSING_CITATION": "CLAIM_NOT_CITED",
    "GENERATION_SCHEMA_VIOLATION": "VERIFIER_MALFORMED_OUTPUT",
    "GENERATION_MALFORMED_RESPONSE": "VERIFIER_MALFORMED_OUTPUT",
    "GENERATION_EMPTY": "VERIFIER_MALFORMED_OUTPUT",
}

MESSAGE = {
    "UNSUPPORTED_CLAIM": (
        "A statement in the draft was not supported by the evidence it cited, so no answer is "
        "released. Abstaining is the intended outcome; the evidence remains available to inspect."
    ),
    "CONTRADICTED_CLAIM": (
        "A statement in the draft conflicted with the evidence it cited — a value, a unit or a "
        "negation did not match. No answer is released."
    ),
    "EVIDENCE_CONFLICT": (
        "The retrieved sources disagree and that disagreement has not been resolved. The competing "
        "evidence is preserved rather than one source being chosen."
    ),
    "CITATION_INVALID": (
        "A citation did not resolve to evidence supplied with this request, so the answer could "
        "not be traced back to its sources."
    ),
    "VERIFICATION_UNAVAILABLE": (
        "Verification could not be completed, so nothing is released as verified. An unverified "
        "answer is not substituted."
    ),
    "REPAIR_FAILED": (
        "A single corrected draft was requested and it still failed verification. No further "
        "attempt is made and no answer is released."
    ),
}


class VerificationService:
    def __init__(
        self,
        generation: GenerationService,
        settings: Settings,
        verifier: ClaimVerifier | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.generation, self.settings = generation, settings
        self._verifier = verifier
        self._provider = provider

    async def answer(
        self,
        actor: Principal,
        query: str,
        correlation_id: UUID,
        filters: RetrievalFilters | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._answer(actor, query, correlation_id, filters)
        except DomainError as exc:
            self._record(correlation_id, exc.code)
            raise

    async def _answer(
        self,
        actor: Principal,
        query: str,
        correlation_id: UUID,
        filters: RetrievalFilters | None,
    ) -> dict[str, Any]:
        actor.require("generation:verify")
        started = perf_counter()
        # M7 owns retrieval, the sufficiency gate and the draft. M8 never re-opens that decision:
        # an INSUFFICIENT or CONFLICTING question stays abstained and no generation is attempted.
        result = await self.generation.draft(actor, query, correlation_id, filters)
        response: dict[str, Any] = {
            **result,
            "mode": "VERIFIED_ANSWER",
            "answering_enabled": False,
            "verified": False,
            "verification": None,
            "verified_answer": None,
            "verification_abstention": None,
        }
        if result["draft"] is None:
            # The gate already refused. Nothing to verify and nothing to release.
            response["durations_ms"]["verification_total_ms"] = 0.0
            return response

        evidence = EvidenceSet.model_validate(result["evidence_set"])
        draft = GroundedDraft.model_validate(result["draft"])
        report, verified, abstention = await self._verify(
            actor, query, evidence, draft, correlation_id
        )
        response["verification"] = report.model_dump(mode="json")
        response["verified"] = verified is not None
        response["verified_answer"] = verified.model_dump(mode="json") if verified else None
        response["verification_abstention"] = (
            abstention.model_dump(mode="json") if abstention else None
        )
        if verified is not None:
            response["draft"] = verified.draft.model_dump(mode="json")
        response["durations_ms"]["verification_total_ms"] = (perf_counter() - started) * 1000
        return response

    async def _verify(
        self,
        actor: Principal,
        query: str,
        evidence: EvidenceSet,
        draft: GroundedDraft,
        correlation_id: UUID,
    ) -> tuple[VerificationReport, VerifiedAnswer | None, VerificationAbstention | None]:
        settings = self.settings
        blocks = list(evidence.evidence_blocks)
        by_id = {str(b.evidence_id): b for b in blocks}
        verifier = self._verifier
        repair_count = 0
        durations: dict[str, float] = {}

        while True:
            started = perf_counter()
            extracted = extraction.extract(draft, by_id, settings.claim_extraction)
            durations["claim_extraction_ms"] = (perf_counter() - started) * 1000

            started = perf_counter()
            try:
                if verifier is None and settings.claim_verification.semantic_verification_enabled:
                    verifier = build_verifier(settings)
                verifications = await verify_claims(
                    extracted, blocks, actor.tenant_id, verifier, settings.claim_verification
                )
            except (VerificationError, GenerationError) as exc:
                # Fail closed. A verifier that could not run has approved nothing.
                return self._abstain(extracted, [], [], repair_count, verifier, durations, exc.code)
            durations["claim_verification_ms"] = (perf_counter() - started) * 1000

            started = perf_counter()
            contradictions = find_contradictions(extracted, blocks, settings.contradiction)
            durations["contradiction_ms"] = (perf_counter() - started) * 1000

            outcome, codes = decide(
                verifications,
                contradictions,
                repair_count,
                settings.repair.enabled,
                settings.final_verification,
            )
            self._log(correlation_id, outcome, codes, repair_count)

            if outcome != "REGENERATE_ONCE":
                report = self._report(
                    outcome,
                    extracted,
                    verifications,
                    contradictions,
                    codes,
                    repair_count,
                    verifier,
                    durations,
                )
                if outcome == "PASS":
                    return (
                        report,
                        self._release(draft, verifications, evidence, repair_count, verifier),
                        None,
                    )
                return (
                    report,
                    None,
                    self._abstention(codes, verifications, contradictions, repair_count),
                )

            started = perf_counter()
            try:
                draft = await self._repair(query, evidence, draft, verifications)
            except (GenerationError, VerificationError) as exc:
                return self._abstain(
                    extracted,
                    verifications,
                    contradictions,
                    repair_count,
                    verifier,
                    durations,
                    exc.code,
                )
            durations["repair_generation_ms"] = (perf_counter() - started) * 1000
            repair_count += 1
            # Loop once more: a repaired draft is re-verified in full, never released on trust.

    async def _repair(
        self,
        query: str,
        evidence: EvidenceSet,
        draft: GroundedDraft,
        verifications: list[ClaimVerification],
    ) -> GroundedDraft:
        grounding = self.settings.grounding
        blocks = evidence.evidence_blocks[: grounding.max_evidence_blocks]
        approved = [b.evidence_id for b in blocks]
        rendered = render_evidence(evidence.evidence_blocks, grounding)
        provider = self._provider or self.generation._provider
        if provider is None:
            from app.generation.providers.factory import build_provider

            provider = build_provider(self.settings)
        failures = [v for v in verifications if v.failed]
        produced = await provider.generate_structured(
            system_policy=REPAIR_POLICY,
            question=repair_message(query, rendered, failures),
            # The same evidence, never more. A repair may only narrow the answer.
            evidence=rendered,
            schema=ProviderDraft,
        )
        cited, uncited = bind(produced, approved)
        spec = provider.specification
        return GroundedDraft(
            draft_id=uuid4(),
            answer=produced.answer,
            claims=produced.claims,
            cited_evidence_ids=cited,
            uncited_evidence_ids=uncited,
            evidence_gap=produced.evidence_gap,
            query_hash=draft.query_hash,
            provider=spec,
            grounding_policy_version=grounding.version,
            grounding_policy_fingerprint=grounding.fingerprint,
            sufficiency_policy_fingerprint=draft.sufficiency_policy_fingerprint,
            durations_ms=draft.durations_ms,
        )

    def _release(
        self,
        draft: GroundedDraft,
        verifications: list[ClaimVerification],
        evidence: EvidenceSet,
        repair_count: int,
        verifier: ClaimVerifier | None,
    ) -> VerifiedAnswer:
        """The only construction of `verified=True` in the system."""
        if verifier is None:
            raise VerificationError("VERIFIER_FAILED", "No verifier produced these verdicts.")
        supported: list[UUID] = []
        for verification in verifications:
            for evidence_id in verification.supporting_evidence_ids:
                if evidence_id not in supported:
                    supported.append(evidence_id)
        return VerifiedAnswer(
            answer_id=uuid4(),
            answer=draft.answer,
            claims=[v for v in verifications if v.material],
            cited_evidence_ids=supported or list(draft.cited_evidence_ids),
            query_hash=evidence.query_hash,
            draft=draft,
            generator=draft.provider,
            verifier=verifier.specification,
            repair_count=repair_count,
        )

    def _abstain(
        self,
        extracted: list[Claim],
        verifications: list[ClaimVerification],
        contradictions: list[ContradictionFinding],
        repair_count: int,
        verifier: ClaimVerifier | None,
        durations: dict[str, float],
        code: str,
    ) -> tuple[VerificationReport, None, VerificationAbstention]:
        # Report the actual cause. A repaired draft that cited invented evidence failed on its
        # citations, and calling that "verification unavailable" would send a reader looking for a
        # broken verifier instead of a broken draft.
        resolved = GENERATION_FAILURE.get(code, code)
        codes: list[ReasonCode] = [
            cast(ReasonCode, resolved) if resolved in ABSTENTION_REASON else "VERIFIER_FAILED"
        ]
        report = self._report(
            "ABSTAIN",
            extracted,
            verifications,
            contradictions,
            codes,
            repair_count,
            verifier,
            durations,
        )
        return report, None, self._abstention(codes, verifications, contradictions, repair_count)

    def _abstention(
        self,
        codes: list[ReasonCode],
        verifications: list[ClaimVerification],
        contradictions: list[ContradictionFinding],
        repair_count: int,
    ) -> VerificationAbstention:
        reason: AbstentionReason = (
            "REPAIR_FAILED"
            if repair_count
            else next(
                (ABSTENTION_REASON[c] for c in codes if c in ABSTENTION_REASON),
                "VERIFICATION_UNAVAILABLE",
            )
        )
        contradicting: list[UUID] = []
        for finding in contradictions:
            contradicting.extend(finding.evidence_ids)
        for verification in verifications:
            contradicting.extend(verification.contradicting_evidence_ids)
        return VerificationAbstention(
            reason=reason,
            reason_codes=list(dict.fromkeys(codes)),
            message=MESSAGE[reason],
            unsupported_claim_ids=[v.claim_id for v in verifications if v.failed],
            contradicting_evidence_ids=list(dict.fromkeys(contradicting)),
        )

    def _report(
        self,
        outcome: Outcome,
        extracted: list[Claim],
        verifications: list[ClaimVerification],
        contradictions: list[ContradictionFinding],
        codes: list[ReasonCode],
        repair_count: int,
        verifier: ClaimVerifier | None,
        durations: dict[str, float],
    ) -> VerificationReport:
        settings = self.settings
        material = [v for v in verifications if v.material]
        return VerificationReport(
            outcome=outcome,
            claims=extracted,
            # Every verdict is retained, including the failures. Dropping them and returning what
            # remains is exactly the transformation that must never happen silently.
            verifications=verifications,
            contradictions=contradictions,
            failed_reason_codes=list(dict.fromkeys(codes)),
            material_claims=len(material),
            supported_claims=sum(v.verdict == "SUPPORTED" for v in material),
            repair_count=repair_count,
            verifier=verifier.specification if verifier else None,
            claim_extraction_fingerprint=settings.claim_extraction.fingerprint,
            claim_verification_fingerprint=settings.claim_verification.fingerprint,
            contradiction_fingerprint=settings.contradiction.fingerprint,
            repair_fingerprint=settings.repair.fingerprint,
            final_policy_fingerprint=settings.final_verification.fingerprint,
            durations_ms=durations,
        )

    def _log(
        self, correlation_id: UUID, outcome: str, codes: list[ReasonCode], repair_count: int
    ) -> None:
        metrics = self.generation.evidence.retrieval.metrics
        if metrics:
            metrics.verification.labels(outcome=outcome).inc()
        # Bounded declared vocabulary only: no question, no claim text, no evidence, no answer.
        logging.getLogger("medical_rag.verification").info(
            "verification_decided",
            extra={
                "event": "verification_decided",
                "request_id": str(correlation_id),
                "outcome": outcome,
                "reason_codes": [str(c) for c in codes],
                "repair_count": repair_count,
            },
        )

    def _record(self, correlation_id: UUID, code: str) -> None:
        metrics = self.generation.evidence.retrieval.metrics
        if metrics:
            metrics.failures.labels(mode="VERIFIED_ANSWER", code=code).inc()
        logging.getLogger("medical_rag.verification").info(
            "verification_failed",
            extra={
                "event": "verification_failed",
                "request_id": str(correlation_id),
                "code": code,
            },
        )
