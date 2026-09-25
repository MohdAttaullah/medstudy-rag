"""M7 orchestration: evidence, then the gate, then — only if the gate permits — a grounded draft.

The ordering here is the safety property. `EvidenceService` produces the M6 EvidenceSet, the gate
decides from its structure whether generation may be attempted at all, and the provider is
constructed only after that decision. A provider failure abstains; it never falls back to an
ungrounded answer or to a different model.
"""

import logging
from time import perf_counter
from typing import Any, Literal
from uuid import UUID, uuid4

from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.errors import DomainError
from app.evidence.model import EvidenceSet
from app.generation.citations.validate import bind
from app.generation.errors import GenerationError
from app.generation.grounding.model import (
    Abstention,
    DeclinedDraft,
    GroundedDraft,
    ProviderResult,
)
from app.generation.prompts.grounded import SYSTEM_POLICY, render_evidence
from app.generation.providers.base import LLMProvider
from app.generation.providers.factory import build_provider
from app.observability.usage import UsageSink
from app.retrieval.model import RetrievalFilters
from app.security.auth import Principal
from app.services.evidence import EvidenceService
from app.services.progress import reporter
from app.sufficiency.gate import SufficiencyGate
from app.sufficiency.model import SufficiencyDecision

ABSTENTION_MESSAGE = {
    "INSUFFICIENT_EVIDENCE": (
        "The indexed evidence does not support an answer to this question. Abstaining is the "
        "intended outcome; the retrieved sources remain available for inspection."
    ),
    "CONFLICTING_EVIDENCE": (
        "The retrieved sources disagree and this conflict has not been resolved. The competing "
        "evidence is preserved below rather than one source being chosen."
    ),
    "GENERATION_FAILED": (
        "A grounded draft could not be produced from the approved evidence. No ungrounded answer "
        "is substituted."
    ),
}


class GenerationService:
    def __init__(
        self,
        evidence: EvidenceService,
        settings: Settings,
        provider: LLMProvider | None = None,
        usage: "UsageSink | None" = None,
    ) -> None:
        self.evidence, self.settings = evidence, settings
        self.gate = SufficiencyGate(settings.sufficiency)
        self._provider = provider
        # Token accounting only. Never consulted when deciding whether an answer is released.
        self._usage = usage

    async def draft(
        self,
        actor: Principal,
        query: str,
        correlation_id: UUID,
        filters: RetrievalFilters | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._draft(actor, query, correlation_id, filters)
        except DomainError as exc:
            self._record(correlation_id, exc.code)
            raise

    async def _draft(
        self,
        actor: Principal,
        query: str,
        correlation_id: UUID,
        filters: RetrievalFilters | None,
    ) -> dict[str, Any]:
        actor.require_any("generation:draft", "ask:submit")
        if len(query) > self.settings.grounding.max_question_chars:
            raise GenerationError(
                "GENERATION_NOT_PERMITTED", "The question exceeds the configured length."
            )
        started = perf_counter()
        # The M6 pipeline is synchronous SQLAlchemy; keep it off the event loop.
        result = await run_in_threadpool(
            self.evidence.search, actor, query, correlation_id, filters
        )
        evidence = EvidenceSet.model_validate(result["evidence_set"])

        gate_started = perf_counter()
        decision = self.gate.evaluate(query, evidence)
        gate_ms = (perf_counter() - gate_started) * 1000
        progress = reporter()
        progress.complete("EVIDENCE")
        self._log_decision(correlation_id, decision)

        response: dict[str, Any] = {
            **result,
            "mode": "GROUNDED_DRAFT",
            # The final Ask experience is M9. A draft is never a delivered answer.
            "answering_enabled": False,
            "verified": False,
            "sufficiency": decision.model_dump(mode="json"),
            "draft": None,
            "abstention": None,
            "durations_ms": {**result["evidence_set"]["reranking_trace"]["durations_ms"]},
        }
        response["durations_ms"]["sufficiency_ms"] = gate_ms

        if not decision.generation_permitted:
            reason: Literal["INSUFFICIENT_EVIDENCE", "CONFLICTING_EVIDENCE"] = (
                "CONFLICTING_EVIDENCE"
                if decision.status == "CONFLICTING"
                else "INSUFFICIENT_EVIDENCE"
            )
            response["abstention"] = Abstention(
                reason=reason,
                reason_codes=list(decision.reason_codes),
                message=ABSTENTION_MESSAGE[reason],
                conflicting_evidence_ids=list(decision.conflicting_evidence_ids),
            ).model_dump(mode="json")
            response["durations_ms"]["pipeline_total_ms"] = (perf_counter() - started) * 1000
            progress.skip("GENERATION", "VERIFICATION")
            return response

        progress.start("GENERATION")
        produced, provider_ms = await self._generate(query, evidence, decision, correlation_id)
        if isinstance(produced, DeclinedDraft):
            # The provider read the evidence and reported that it does not address the question.
            # That is a statement about the corpus, so it abstains semantically rather than being
            # recorded as a technical failure — and it carries no answer and no claims, so there
            # is nothing for M8 to verify. The provider's only new power is refusal: it cannot
            # reopen the gate, release anything, or bypass verification. See ADR-022.
            response["abstention"] = Abstention(
                reason="INSUFFICIENT_EVIDENCE",
                reason_codes=[
                    *decision.reason_codes,
                    "EVIDENCE_DOES_NOT_ADDRESS_QUESTION",
                ],
                message=ABSTENTION_MESSAGE["INSUFFICIENT_EVIDENCE"],
            ).model_dump(mode="json")
            response["durations_ms"]["generation_ms"] = provider_ms
            response["durations_ms"]["pipeline_total_ms"] = (perf_counter() - started) * 1000
            progress.complete("GENERATION")
            progress.skip("VERIFICATION")
            return response

        progress.complete("GENERATION")
        response["draft"] = produced.model_dump(mode="json")
        response["durations_ms"]["generation_ms"] = produced.durations_ms["provider_ms"]
        response["durations_ms"]["pipeline_total_ms"] = (perf_counter() - started) * 1000
        return response

    async def _generate(
        self,
        query: str,
        evidence: EvidenceSet,
        decision: SufficiencyDecision,
        correlation_id: UUID,
    ) -> tuple[GroundedDraft | DeclinedDraft, float]:
        grounding = self.settings.grounding
        blocks = evidence.evidence_blocks[: grounding.max_evidence_blocks]
        approved = [b.evidence_id for b in blocks]
        rendered = render_evidence(evidence.evidence_blocks, grounding)
        provider = self._provider or build_provider(self.settings, self._usage)

        started = perf_counter()
        result = await provider.generate_structured(
            system_policy=SYSTEM_POLICY,
            question=query,
            evidence=rendered,
            schema=ProviderResult,
        )
        provider_ms = (perf_counter() - started) * 1000
        produced = result.result
        if isinstance(produced, DeclinedDraft):
            spec = provider.specification
            self._record(correlation_id, None, provider=spec.provider, model=spec.model_id)
            return produced, provider_ms
        if len(produced.answer) > grounding.max_answer_chars:
            raise GenerationError(
                "GENERATION_SCHEMA_VIOLATION", "The draft exceeds the answer limit."
            )
        if len(produced.claims) > grounding.max_claims:
            raise GenerationError(
                "GENERATION_SCHEMA_VIOLATION", "The draft exceeds the claim limit."
            )

        cited, uncited = bind(produced, approved)
        spec = provider.specification
        self._record(correlation_id, None, provider=spec.provider, model=spec.model_id)
        return GroundedDraft(
            draft_id=uuid4(),
            answer=produced.answer,
            claims=produced.claims,
            cited_evidence_ids=cited,
            uncited_evidence_ids=uncited,
            evidence_gap=produced.evidence_gap,
            query_hash=evidence.query_hash,
            provider=spec,
            grounding_policy_version=grounding.version,
            grounding_policy_fingerprint=grounding.fingerprint,
            sufficiency_policy_fingerprint=decision.policy_fingerprint,
            durations_ms={"provider_ms": provider_ms},
        ), provider_ms

    def _log_decision(self, correlation_id: UUID, decision: SufficiencyDecision) -> None:
        metrics = self.evidence.retrieval.metrics
        if metrics:
            metrics.sufficiency.labels(status=decision.status).inc()
        # Reason codes are a bounded declared vocabulary; the question and the evidence are not
        # logged, in line with the M5 rule that medical queries stay out of telemetry.
        logging.getLogger("medical_rag.generation").info(
            "sufficiency_decided",
            extra={
                "event": "sufficiency_decided",
                "request_id": str(correlation_id),
                "status": decision.status,
                "question_kind": decision.question_kind,
                "reason_codes": list(decision.reason_codes),
                "policy_fingerprint": decision.policy_fingerprint,
            },
        )

    def _record(
        self,
        correlation_id: UUID,
        code: str | None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        metrics = self.evidence.retrieval.metrics
        if code and metrics:
            metrics.failures.labels(mode="GROUNDED_DRAFT", code=code).inc()
        logging.getLogger("medical_rag.generation").info(
            "draft_failed" if code else "draft_produced",
            extra={
                "event": "draft_failed" if code else "draft_produced",
                "request_id": str(correlation_id),
                **({"code": code} if code else {}),
                **({"provider": provider, "model": model} if provider else {}),
            },
        )
