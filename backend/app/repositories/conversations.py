"""Conversation persistence, scoped by tenant on every read and every write.

Every query filters on `tenant_id` from the authenticated principal, never from a route parameter.
A conversation id taken from a URL is treated as an untrusted string: it narrows a search that is
already restricted to the caller's tenant, so guessing another tenant's id finds nothing rather than
finding something and then being rejected.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models.conversations import Conversation, ConversationTurn, TurnCitation


def _not_found() -> DomainError:
    # The same error whether the conversation belongs to another tenant or does not exist. A
    # distinguishable "forbidden" would confirm the id is real to someone probing for it.
    return DomainError("CONVERSATION_NOT_FOUND", "No such conversation.", 404)


class ConversationRepository:
    def __init__(self, session: Session, tenant_id: UUID, user_id: UUID) -> None:
        self.session, self.tenant_id, self.user_id = session, tenant_id, user_id

    def get(self, conversation_id: UUID) -> Conversation:
        row = self.session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self.tenant_id,
                Conversation.archived_at.is_(None),
            )
        )
        if row is None:
            raise _not_found()
        return row

    def open(self, conversation_id: UUID | None, question: str, title_chars: int) -> Conversation:
        if conversation_id is not None:
            return self.get(conversation_id)
        title = question.strip().splitlines()[0][:title_chars] or "Untitled question"
        row = Conversation(
            id=uuid4(),
            tenant_id=self.tenant_id,
            created_by_user_id=self.user_id,
            title=title,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def existing_turn(self, key: str) -> ConversationTurn | None:
        """A retry returns its stored turn rather than spending another provider call."""
        return self.session.scalar(
            select(ConversationTurn).where(
                ConversationTurn.tenant_id == self.tenant_id,
                ConversationTurn.idempotency_key == key,
            )
        )

    def next_sequence(self, conversation_id: UUID) -> int:
        highest = self.session.scalar(
            select(func.max(ConversationTurn.sequence_number)).where(
                ConversationTurn.conversation_id == conversation_id,
                ConversationTurn.tenant_id == self.tenant_id,
            )
        )
        return (highest or 0) + 1

    def record(
        self,
        conversation: Conversation,
        *,
        key: str,
        correlation_id: UUID,
        question: str,
        outcome: str,
        answer: str | None,
        reason_codes: list[str],
        sufficiency_status: str | None,
        verification_outcome: str | None,
        repair_count: int,
        generator_model: str | None,
        verifier_model: str | None,
        verifier_independent: bool | None,
        durations: dict[str, Any],
        citations: list[dict[str, Any]],
    ) -> ConversationTurn:
        turn = ConversationTurn(
            id=uuid4(),
            tenant_id=self.tenant_id,
            conversation_id=conversation.id,
            created_by_user_id=self.user_id,
            sequence_number=self.next_sequence(conversation.id),
            idempotency_key=key,
            correlation_id=correlation_id,
            question_text=question,
            outcome=outcome,
            # The database CHECK ties these three together; passing them separately would let an
            # unverified answer be written if this method were ever edited carelessly.
            verified=outcome == "VERIFIED",
            answer_text=answer if outcome == "VERIFIED" else None,
            reason_codes=reason_codes,
            sufficiency_status=sufficiency_status,
            verification_outcome=verification_outcome,
            repair_count=repair_count,
            generator_model=generator_model,
            verifier_model=verifier_model,
            verifier_independent=verifier_independent,
            durations_ms=durations,
        )
        self.session.add(turn)
        self.session.flush()
        for ordinal, citation in enumerate(citations, 1):
            self.session.add(
                TurnCitation(
                    id=uuid4(),
                    tenant_id=self.tenant_id,
                    turn_id=turn.id,
                    ordinal=ordinal,
                    **citation,
                )
            )
        conversation.updated_at = datetime.now(UTC)
        self.session.flush()
        return turn

    def turns(self, conversation_id: UUID) -> list[ConversationTurn]:
        self.get(conversation_id)
        return list(
            self.session.scalars(
                select(ConversationTurn)
                .where(
                    ConversationTurn.conversation_id == conversation_id,
                    ConversationTurn.tenant_id == self.tenant_id,
                )
                .order_by(ConversationTurn.sequence_number)
            ).all()
        )

    def citations(self, turn_id: UUID) -> list[TurnCitation]:
        return list(
            self.session.scalars(
                select(TurnCitation)
                .where(
                    TurnCitation.turn_id == turn_id,
                    TurnCitation.tenant_id == self.tenant_id,
                )
                .order_by(TurnCitation.ordinal)
            ).all()
        )

    def list_conversations(self, limit: int, offset: int) -> list[tuple[Conversation, int, int]]:
        verified = func.count(ConversationTurn.id).filter(ConversationTurn.verified.is_(True))
        rows = self.session.execute(
            select(Conversation, func.count(ConversationTurn.id), verified)
            .outerjoin(
                ConversationTurn,
                (ConversationTurn.conversation_id == Conversation.id)
                & (ConversationTurn.tenant_id == Conversation.tenant_id),
            )
            .where(
                Conversation.tenant_id == self.tenant_id,
                Conversation.archived_at.is_(None),
            )
            .group_by(Conversation.id)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [(row[0], row[1], row[2]) for row in rows]

    def count(self) -> int:
        return (
            self.session.scalar(
                select(func.count(Conversation.id)).where(
                    Conversation.tenant_id == self.tenant_id,
                    Conversation.archived_at.is_(None),
                )
            )
            or 0
        )
