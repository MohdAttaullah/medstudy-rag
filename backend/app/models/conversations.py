"""M9 conversation history.

The first milestone since M1 that genuinely needs new durable state: a conversation the user can
come back to must outlive the request that produced it. What is stored is deliberately narrow — the
question, the outcome, the answer *only when it was verified*, and the identity of the sources that
supported it. Prompts, provider responses, drafts that failed verification, verifier reasoning and
request-scoped EvidenceSets are not persisted; none of them is something a user needs to re-read and
every one of them is something a store should not accumulate.

Every table carries `tenant_id` and is reachable only through a composite key that includes it, so
ownership is a property of the row rather than of the query that happened to find it.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDTimestampMixin

# The outcomes a user can be shown. Stored as text with a CHECK rather than a native enum, matching
# the ingestion-status convention, so adding an outcome later is a migration rather than a type
# rewrite.
OUTCOMES = (
    "VERIFIED",
    "INSUFFICIENT_EVIDENCE",
    "CONFLICTING_EVIDENCE",
    "UNVERIFIED",
    "FAILED",
)


class Conversation(UUIDTimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        ForeignKeyConstraint(["created_by_user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    created_by_user_id: Mapped[UUID]
    # Derived from the first question rather than generated, so no model output names a user's
    # conversation.
    title: Mapped[str] = mapped_column(String(200))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationTurn(UUIDTimestampMixin, Base):
    """One question and what the pipeline decided about it."""

    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id"], ["conversations.id", "conversations.tenant_id"]
        ),
        ForeignKeyConstraint(["created_by_user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
        CheckConstraint(
            "outcome IN " + str(OUTCOMES),
            name="turn_outcome",
        ),
        # The safety invariant, enforced by the database rather than only by application code: an
        # answer exists only on a verified turn, and a verified turn has one. A failed draft can
        # never be stored where a verified answer is read from.
        CheckConstraint(
            "(outcome = 'VERIFIED') = (answer_text IS NOT NULL)",
            name="answer_only_when_verified",
        ),
        CheckConstraint(
            "(outcome = 'VERIFIED') = verified",
            name="verified_matches_outcome",
        ),
        CheckConstraint("repair_count >= 0 AND repair_count <= 1", name="bounded_repair"),
        UniqueConstraint("conversation_id", "sequence_number", name="uq_turn_sequence"),
        # One submission is one turn. A retry that reuses the key returns the existing turn instead
        # of spending another provider call.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_turn_idempotency"),
    )
    configuration_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    conversation_id: Mapped[UUID] = mapped_column(index=True)
    created_by_user_id: Mapped[UUID]
    sequence_number: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    correlation_id: Mapped[UUID]
    question_text: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(24))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Present only on a VERIFIED turn; the CHECK above makes the two inseparable.
    answer_text: Mapped[str | None] = mapped_column(Text)
    # Declared vocabulary only — the same reason codes M7 and M8 already publish.
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sufficiency_status: Mapped[str | None] = mapped_column(String(24))
    verification_outcome: Mapped[str | None] = mapped_column(String(24))
    repair_count: Mapped[int] = mapped_column(Integer, default=0)
    # Model identity for traceability. Configuration, not a secret, and never a key.
    generator_model: Mapped[str | None] = mapped_column(String(200))
    verifier_model: Mapped[str | None] = mapped_column(String(200))
    verifier_independent: Mapped[bool | None] = mapped_column(Boolean)
    durations_ms: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class TurnCitation(UUIDTimestampMixin, Base):
    """One source that supported a verified answer, kept resolvable and kept honest.

    The cited text is stored rather than re-resolved at read time. A verified answer was verified
    against the exact words below; if the document were later re-parsed, re-resolution would quietly
    change what a stored answer appears to cite, which is precisely the drift provenance exists to
    prevent. The identifiers alongside it still resolve to the live document for inspection.
    """

    __tablename__ = "turn_citations"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        ForeignKeyConstraint(
            ["turn_id", "tenant_id"], ["conversation_turns.id", "conversation_turns.tenant_id"]
        ),
        ForeignKeyConstraint(["document_id", "tenant_id"], ["documents.id", "documents.tenant_id"]),
        UniqueConstraint("turn_id", "evidence_id", name="uq_citation_evidence"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    turn_id: Mapped[UUID] = mapped_column(index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    # The request-scoped M6 evidence id, kept so a stored citation can be matched back to the
    # claim that cited it within the same turn.
    evidence_id: Mapped[UUID]
    document_id: Mapped[UUID]
    document_version_id: Mapped[UUID]
    chunk_run_id: Mapped[UUID]
    parse_run_id: Mapped[UUID]
    document_title: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(30))
    authority_level: Mapped[str] = mapped_column(String(30))
    chunk_type: Mapped[str] = mapped_column(String(40))
    pages: Mapped[list[int]] = mapped_column(JSONB, default=list)
    source_element_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Bounding boxes and roles as M2 recorded them, so a citation can highlight the real region
    # rather than a fabricated one.
    source_spans: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    cited_text: Mapped[str] = mapped_column(Text)
