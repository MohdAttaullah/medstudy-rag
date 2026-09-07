"""M9 Ask orchestration: one authorized path from a question to a shown answer, or to a refusal.

This service adds no decision of its own. It calls the existing M5→M8 pipeline, reads the outcome
those stages already reached, records the turn, and shapes a public response. Every rule about
whether a question *can* be answered lives upstream; duplicating any of it would create a second
place for the answer rule to drift, and the second place is the one that gets it wrong.

The only thing this file decides is what a reader is shown — and there the rule is single-valued:
an answer is released when, and only when, M8 returned PASS.
"""

import logging
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.errors import DomainError
from app.models.conversations import Conversation, ConversationTurn, TurnCitation
from app.repositories.conversations import ConversationRepository
from app.retrieval.model import RetrievalFilters
from app.schemas.ask import (
    AskCitation,
    AskClaim,
    AskResponse,
    AskSource,
    CitationArtifact,
    CitationSpan,
    ConversationTurnView,
    StageTiming,
)
from app.security.auth import Principal
from app.services.verification import VerificationService

# One sentence per outcome, chosen here rather than anywhere near a model. A reader must be able to
# tell missing evidence from conflicting evidence from a failed check from a broken provider,
# because those call for four different next actions.
MESSAGES = {
    "VERIFIED": ("Every statement below was checked against the sources cited with it."),
    "INSUFFICIENT_EVIDENCE": (
        "The indexed sources do not support an answer to this question. Nothing has been filled in "
        "from the model's own knowledge, and no answer is shown."
    ),
    "CONFLICTING_EVIDENCE": (
        "The indexed sources disagree with each other about this question, and that disagreement "
        "has not been resolved. No single answer is shown."
    ),
    "UNVERIFIED": (
        "Evidence was found, but a supported answer could not be verified against it. The draft is "
        "not shown."
    ),
    "FAILED": (
        "The answering service could not complete this request. This is a technical failure, not a "
        "statement about the evidence."
    ),
}

# Declared failure codes that mean the infrastructure broke, so the reader is told that rather than
# being told the corpus lacked evidence.
PROVIDER_FAILURES = frozenset(
    {
        "GENERATION_PROVIDER_UNCONFIGURED",
        "GENERATION_PROVIDER_UNAVAILABLE",
        "GENERATION_PROVIDER_TIMEOUT",
        "GENERATION_PROVIDER_AUTH_FAILED",
        "GENERATION_PROVIDER_REJECTED_REQUEST",
        "GENERATION_RATE_LIMITED",
        "GENERATION_MALFORMED_RESPONSE",
        "GENERATION_SCHEMA_VIOLATION",
        "GENERATION_UNKNOWN_CITATION",
        "GENERATION_MISSING_CITATION",
        "GENERATION_EMPTY",
        "VERIFIER_FAILED",
        "VERIFIER_MALFORMED_OUTPUT",
        "VERIFIER_UNKNOWN_EVIDENCE",
    }
)

STAGE_LABELS = {
    "corpus_ms": "Resolving sources",
    "encoding_ms": "Searching sources",
    "dense_ms": "Searching sources",
    "sparse_ms": "Searching sources",
    "fusion_ms": "Combining results",
    "hydration_ms": "Loading sources",
    "reranking_ms": "Reranking evidence",
    "expansion_ms": "Expanding context",
    "assembly_ms": "Assembling evidence",
    "sufficiency_ms": "Checking evidence sufficiency",
    "generation_ms": "Drafting from evidence",
    "verification_total_ms": "Verifying claims",
    "pipeline_total_ms": "Total",
}


class AskService:
    def __init__(self, verification: VerificationService, settings: Settings) -> None:
        self.verification, self.settings = verification, settings

    async def ask(
        self,
        actor: Principal,
        question: str,
        correlation_id: UUID,
        conversation_id: UUID | None = None,
        idempotency_key: str | None = None,
        filters: RetrievalFilters | None = None,
    ) -> AskResponse:
        actor.require("ask:submit")
        config = self.settings.ask
        if len(question) > config.max_question_chars:
            raise DomainError("QUESTION_TOO_LONG", "The question exceeds the allowed length.", 422)
        key = idempotency_key or uuid4().hex

        if conversation_id is not None:
            # Resolve ownership before anything expensive. Discovering that a conversation is not
            # the caller's only after a generator and a verifier have run would spend two provider
            # calls on a request that was never going to be answered.
            await run_in_threadpool(self._require_conversation, actor, conversation_id)

        stored = await run_in_threadpool(self._replay, actor, key)
        if stored is not None:
            # A retry returns what the first submission decided. Re-running would spend another
            # provider call and could return a different outcome for the same question.
            return stored

        started = perf_counter()
        outcome, payload, failure = "FAILED", {}, None
        try:
            payload = await self.verification.answer(actor, question, correlation_id, filters)
            outcome = self._outcome(payload)
        except DomainError as exc:
            failure = exc.code
            outcome = "FAILED" if exc.code in PROVIDER_FAILURES else "FAILED"
            if exc.code in ("FORBIDDEN", "UNAUTHORIZED"):
                raise
        total = (perf_counter() - started) * 1000

        response = await run_in_threadpool(
            self._commit,
            actor,
            key,
            correlation_id,
            conversation_id,
            question,
            outcome,
            payload,
            failure,
            total,
        )
        self._log(correlation_id, response.outcome, response.reason_codes)
        return response

    @staticmethod
    def _outcome(payload: dict[str, Any]) -> str:
        """Read the outcome the pipeline already reached. Nothing is re-decided here."""
        if payload.get("verified") and payload.get("verified_answer"):
            return "VERIFIED"
        sufficiency = (payload.get("sufficiency") or {}).get("status")
        if sufficiency == "CONFLICTING":
            return "CONFLICTING_EVIDENCE"
        if sufficiency != "SUFFICIENT":
            return "INSUFFICIENT_EVIDENCE"
        # The gate allowed generation, so a draft existed and verification refused it.
        return "UNVERIFIED"

    def _require_conversation(self, actor: Principal, conversation_id: UUID) -> None:
        with self.verification.generation.evidence.retrieval.sessions() as session:
            ConversationRepository(session, actor.tenant_id, actor.user_id).get(conversation_id)

    def _replay(self, actor: Principal, key: str) -> AskResponse | None:
        with self.verification.generation.evidence.retrieval.sessions() as session:
            repository = ConversationRepository(session, actor.tenant_id, actor.user_id)
            turn = repository.existing_turn(key)
            if turn is None:
                return None
            citations = repository.citations(turn.id)
            return self._response(turn, citations, stages=[])

    def _commit(
        self,
        actor: Principal,
        key: str,
        correlation_id: UUID,
        conversation_id: UUID | None,
        question: str,
        outcome: str,
        payload: dict[str, Any],
        failure: str | None,
        total: float,
    ) -> AskResponse:
        answer = (payload.get("verified_answer") or {}) if outcome == "VERIFIED" else {}
        report = payload.get("verification") or {}
        blocks = {
            b["evidence_id"]: b
            for b in ((payload.get("evidence_set") or {}).get("evidence_blocks") or [])
        }
        citations = self._citations(answer, blocks) if outcome == "VERIFIED" else []
        reasons = self._reasons(outcome, payload, report, failure)

        with self.verification.generation.evidence.retrieval.sessions.begin() as session:
            repository = ConversationRepository(session, actor.tenant_id, actor.user_id)
            conversation = repository.open(
                conversation_id, question, self.settings.ask.conversation_title_chars
            )
            verifier = report.get("verifier") or {}
            turn = repository.record(
                conversation,
                key=key,
                correlation_id=correlation_id,
                question=question,
                outcome=outcome,
                answer=answer.get("answer"),
                reason_codes=reasons,
                sufficiency_status=(payload.get("sufficiency") or {}).get("status"),
                verification_outcome=report.get("outcome"),
                repair_count=int(report.get("repair_count") or 0),
                generator_model=((payload.get("draft") or {}).get("provider") or {}).get(
                    "model_id"
                ),
                verifier_model=verifier.get("model_id"),
                verifier_independent=verifier.get("independent_of_generator"),
                durations=payload.get("durations_ms") or {"pipeline_total_ms": total},
                citations=citations,
            )
            stored = repository.citations(turn.id)
            claims = self._claims(answer, stored) if outcome == "VERIFIED" else []
            return self._response(turn, stored, self._stages(payload, total), claims)

    def _citations(self, answer: dict[str, Any], blocks: dict[str, Any]) -> list[dict[str, Any]]:
        """Only evidence that actually supported a verified claim becomes a citation.

        Retrieval candidates that reached the EvidenceSet but supported nothing are diagnostics, and
        showing them beside an answer would imply the answer rests on them.
        """
        rows: list[dict[str, Any]] = []
        for evidence_id in answer.get("cited_evidence_ids") or []:
            block = blocks.get(str(evidence_id))
            if block is None:
                continue
            rows.append(
                {
                    "evidence_id": UUID(str(evidence_id)),
                    "document_id": UUID(block["document_id"]),
                    "document_version_id": UUID(block["document_version_id"]),
                    "chunk_run_id": UUID(block["chunk_run_id"]),
                    "parse_run_id": UUID(block["parse_run_id"]),
                    "document_title": block["document_title"],
                    "source_type": block["source_type"],
                    "authority_level": block["authority_level"],
                    "chunk_type": block["chunk_type"],
                    "pages": list(block.get("pages") or []),
                    "source_element_ids": [str(v) for v in block.get("source_element_ids") or []],
                    "source_spans": [
                        {
                            "element_id": str(span["element_id"]),
                            "page": span.get("page"),
                            "start": span["start"],
                            "end": span["end"],
                            "role": span.get("role") or "PRIMARY",
                            # Kept exactly as M2 recorded it, including its absence.
                            "bbox": span.get("bbox"),
                        }
                        for span in block.get("source_spans") or []
                    ],
                    "artifacts": [
                        {
                            "artifact_id": str(a["artifact_id"]),
                            "kind": a["kind"],
                            "row_indexes": list(a.get("row_indexes") or []),
                            "header_rows": list(a.get("header_rows") or []),
                            "image_available": bool(a.get("image_available")),
                        }
                        for a in block.get("artifacts") or []
                    ],
                    "cited_text": block["text"],
                }
            )
        return rows

    @staticmethod
    def _claims(answer: dict[str, Any], stored: list[TurnCitation]) -> list[AskClaim]:
        by_evidence = {str(row.evidence_id): row.id for row in stored}
        claims = []
        for claim in answer.get("claims") or []:
            ids = [
                by_evidence[str(v)]
                for v in claim.get("supporting_evidence_ids") or []
                if str(v) in by_evidence
            ]
            claims.append(AskClaim(text=claim["claim_text"], citation_ids=ids))
        return claims

    @staticmethod
    def _reasons(
        outcome: str, payload: dict[str, Any], report: dict[str, Any], failure: str | None
    ) -> list[str]:
        if failure:
            return [failure]
        if outcome == "VERIFIED":
            return []
        codes = list(report.get("failed_reason_codes") or [])
        if not codes:
            codes = list((payload.get("sufficiency") or {}).get("reason_codes") or [])
        return codes

    @staticmethod
    def _stages(payload: dict[str, Any], total: float) -> list[StageTiming]:
        durations = payload.get("durations_ms") or {}
        stages = [
            StageTiming(stage=STAGE_LABELS[name], duration_ms=round(float(value), 2))
            for name, value in durations.items()
            if name in STAGE_LABELS
        ]
        if not any(s.stage == "Total" for s in stages):
            stages.append(StageTiming(stage="Total", duration_ms=round(total, 2)))
        return stages

    def _response(
        self,
        turn: ConversationTurn,
        citations: list[TurnCitation],
        stages: list[StageTiming],
        claims: list[AskClaim] | None = None,
    ) -> AskResponse:
        views = [self._citation(row) for row in citations]
        return AskResponse(
            correlation_id=turn.correlation_id,
            conversation_id=turn.conversation_id,
            turn_id=turn.id,
            question=turn.question_text,
            outcome=turn.outcome,  # type: ignore[arg-type]
            verified=turn.verified,
            # The schema validator refuses any combination other than answer-with-VERIFIED.
            answer=turn.answer_text,
            claims=claims or self._stored_claims(turn, views),
            citations=views,
            sources=self._sources(views),
            message=MESSAGES[turn.outcome],
            reason_codes=list(turn.reason_codes or []),
            stages=stages,
            created_at=turn.created_at.isoformat(),
        )

    @staticmethod
    def _stored_claims(turn: ConversationTurn, views: list[AskCitation]) -> list[AskClaim]:
        # A replayed turn shows the answer and its sources; per-claim binding is reconstructed as a
        # single grouping rather than re-derived from an EvidenceSet that no longer exists.
        if turn.outcome != "VERIFIED" or not views:
            return []
        return [AskClaim(text=turn.answer_text or "", citation_ids=[v.citation_id for v in views])]

    @staticmethod
    def _citation(row: TurnCitation) -> AskCitation:
        return AskCitation(
            citation_id=row.id,
            ordinal=row.ordinal,
            document_id=row.document_id,
            document_version_id=row.document_version_id,
            parse_run_id=row.parse_run_id,
            chunk_run_id=row.chunk_run_id,
            document_title=row.document_title,
            source_type=row.source_type,
            authority_level=row.authority_level,
            chunk_type=row.chunk_type,
            pages=list(row.pages or []),
            spans=[
                CitationSpan(
                    element_id=UUID(span["element_id"]),
                    page=span.get("page"),
                    start=span["start"],
                    end=span["end"],
                    role=span.get("role") or "PRIMARY",
                    bbox=(
                        tuple(span["bbox"])
                        if span.get("bbox") and any(v is not None for v in span["bbox"])
                        else None
                    ),
                )
                for span in row.source_spans or []
            ],
            artifacts=[
                CitationArtifact(
                    artifact_id=UUID(a["artifact_id"]),
                    kind=a["kind"],
                    row_indexes=list(a.get("row_indexes") or []),
                    header_rows=list(a.get("header_rows") or []),
                    image_available=bool(a.get("image_available")),
                )
                for a in row.artifacts or []
            ],
            cited_text=row.cited_text,
        )

    @staticmethod
    def _sources(citations: list[AskCitation]) -> list[AskSource]:
        grouped: dict[UUID, AskSource] = {}
        for citation in citations:
            existing = grouped.get(citation.document_version_id)
            if existing is None:
                grouped[citation.document_version_id] = AskSource(
                    document_id=citation.document_id,
                    document_version_id=citation.document_version_id,
                    parse_run_id=citation.parse_run_id,
                    title=citation.document_title,
                    source_type=citation.source_type,
                    authority_level=citation.authority_level,
                    pages=list(citation.pages),
                    citation_ids=[citation.citation_id],
                )
                continue
            grouped[citation.document_version_id] = existing.model_copy(
                update={
                    "pages": sorted(set(existing.pages) | set(citation.pages)),
                    "citation_ids": [*existing.citation_ids, citation.citation_id],
                }
            )
        return list(grouped.values())

    def conversation_turn(
        self, turn: ConversationTurn, citations: list[TurnCitation]
    ) -> ConversationTurnView:
        views = [self._citation(row) for row in citations]
        return ConversationTurnView(
            turn_id=turn.id,
            sequence_number=turn.sequence_number,
            question=turn.question_text,
            outcome=turn.outcome,  # type: ignore[arg-type]
            verified=turn.verified,
            answer=turn.answer_text,
            message=MESSAGES[turn.outcome],
            reason_codes=list(turn.reason_codes or []),
            citations=views if turn.verified else [],
            sources=self._sources(views) if turn.verified else [],
            created_at=turn.created_at.isoformat(),
        )

    def _log(self, correlation_id: UUID, outcome: str, codes: list[str]) -> None:
        metrics = self.verification.generation.evidence.retrieval.metrics
        if metrics:
            metrics.ask.labels(outcome=outcome).inc()
        # Bounded vocabulary only. The question, the answer and the sources stay out of telemetry,
        # exactly as they have since M5.
        logging.getLogger("medical_rag.ask").info(
            "ask_completed",
            extra={
                "event": "ask_completed",
                "request_id": str(correlation_id),
                "outcome": outcome,
                "reason_codes": codes,
            },
        )


def conversation_summary(conversation: Conversation, turns: int, verified: int) -> dict[str, Any]:
    return {
        "conversation_id": conversation.id,
        "title": conversation.title,
        "turn_count": turns,
        "verified_turns": verified,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }
