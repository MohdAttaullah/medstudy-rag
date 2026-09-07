"""Append-only tenant policy revisions; full allowlisted snapshots, never credentials."""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDTimestampMixin


class ConfigurationRevision(UUIDTimestampMixin, Base):
    __tablename__ = "configuration_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "revision"),
        ForeignKeyConstraint(["actor_id", "tenant_id"], ["users.id", "users.tenant_id"]),
        CheckConstraint("revision > 0", name="positive_revision"),
        CheckConstraint("result IN ('ACTIVE', 'PENDING_REBUILD')", name="configuration_result"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID]
    correlation_id: Mapped[UUID]
    revision: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(500))
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    runtime_values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    desired_values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    effective_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    startup_fingerprint: Mapped[str] = mapped_column(String(64))
