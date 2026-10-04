from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
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
from app.models.enums import Authority, SourceType, Status


def enum_type(enum: type, name: str) -> Enum:
    return Enum(enum, name=name, native_enum=False, create_constraint=True, validate_strings=True)


class Tenant(UUIDTimestampMixin, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(120))


class User(UUIDTimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("id", "tenant_id"),)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(30))


class Document(UUIDTimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        ForeignKeyConstraint(["created_by_user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[SourceType] = mapped_column(enum_type(SourceType, "source_type"))
    specialty: Mapped[str | None] = mapped_column(String(120))
    subject: Mapped[str | None] = mapped_column(String(120))
    publisher: Mapped[str | None] = mapped_column(String(200))
    authority_level: Mapped[Authority] = mapped_column(enum_type(Authority, "authority_level"))
    created_by_user_id: Mapped[UUID]
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentVersion(UUIDTimestampMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number"),
        UniqueConstraint("tenant_id", "sha256"),
        UniqueConstraint("id", "tenant_id"),
        ForeignKeyConstraint(["document_id", "tenant_id"], ["documents.id", "documents.tenant_id"]),
        ForeignKeyConstraint(["created_by_user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
        CheckConstraint("NOT searchable", name="m1_never_searchable"),
        CheckConstraint("version_number > 0 AND file_size_bytes > 0", name="positive_version_file"),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_id: Mapped[UUID] = mapped_column(index=True)
    version_number: Mapped[int]
    edition: Mapped[str | None] = mapped_column(String(120))
    publication_year: Mapped[int | None]
    original_filename: Mapped[str] = mapped_column(String(255))
    normalized_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(80))
    file_size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    object_storage_key: Mapped[str] = mapped_column(String(500), unique=True)
    object_version_id: Mapped[str] = mapped_column(String(200))
    page_count: Mapped[int | None]
    ingestion_status: Mapped[Status] = mapped_column(enum_type(Status, "version_status"))
    searchable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_by_user_id: Mapped[UUID]
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionJob(UUIDTimestampMixin, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        UniqueConstraint("document_version_id"),
        ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["document_versions.id", "document_versions.tenant_id"],
        ),
        ForeignKeyConstraint(
            ["requested_by_user_id", "tenant_id"], ["users.id", "users.tenant_id"]
        ),
        CheckConstraint("retry_count >= 0 AND retry_count <= max_retries", name="bounded_retries"),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_version_id: Mapped[UUID]
    status: Mapped[Status] = mapped_column(enum_type(Status, "job_status"), index=True)
    current_stage: Mapped[str] = mapped_column(String(40))
    requested_by_user_id: Mapped[UUID]
    configuration_version: Mapped[str] = mapped_column(String(80))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[UUID]
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queue_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int]
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_error_message: Mapped[str | None] = mapped_column(String(300))


class IngestionStageEvent(UUIDTimestampMixin, Base):
    __tablename__ = "ingestion_stage_events"
    __table_args__ = (UniqueConstraint("ingestion_job_id", "sequence"),)
    sequence: Mapped[int]
    ingestion_job_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_jobs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40))
    service_identity: Mapped[str] = mapped_column(String(120))
    retry_number: Mapped[int]
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(String(300))
    correlation_id: Mapped[UUID]


class AuditEvent(UUIDTimestampMixin, Base):
    __tablename__ = "audit_events"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    resource_id: Mapped[UUID | None]
    correlation_id: Mapped[UUID]
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class UploadIntent(UUIDTimestampMixin, Base):
    __tablename__ = "upload_intents"
    __table_args__ = (UniqueConstraint("tenant_id", "actor_id", "idempotency_key"),)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[UUID]
    fingerprint: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    document_id: Mapped[UUID]
    version_id: Mapped[UUID]
    job_id: Mapped[UUID]
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    cleanup_required: Mapped[bool] = mapped_column(default=False)
    correlation_id: Mapped[UUID]
    __mapper_args__ = {"eager_defaults": True}


class OutboxMessage(UUIDTimestampMixin, Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (UniqueConstraint("job_id", "generation", "kind"),)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_jobs.id"), index=True)
    generation: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20), default="PARSING", server_default="PARSING")
    chunk_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("chunk_runs.id"))
    embedding_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("embedding_runs.id"))
    sparse_index_id: Mapped[UUID | None] = mapped_column(ForeignKey("sparse_indexes.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80))


class DocumentDeletion(UUIDTimestampMixin, Base):
    """The record that a document was permanently deleted, and the state of that deletion.

    It is also the workflow's memory. Deletion reaches three stores that share no transaction —
    object storage, the vector index and PostgreSQL — so a request can fail between them. While a
    row here is not COMPLETED the document stays withdrawn from retrieval and a retry resumes the
    work; once COMPLETED it is the only trace left, and it holds identifiers, timestamps, the
    deleting principal and counts — never a title, text or storage key — so keeping it retains none
    of the deleted content. See ADR-026.
    """

    __tablename__ = "document_deletions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", name="uq_document_deletion"),
        CheckConstraint("status IN ('REQUESTED', 'COMPLETED', 'FAILED')", name="deletion_status"),
        CheckConstraint("attempts >= 1", name="deletion_attempts"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Not a foreign key: this row outlives the document it records.
    document_id: Mapped[UUID]
    requested_by_user_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(16))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[UUID]
    removed: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
