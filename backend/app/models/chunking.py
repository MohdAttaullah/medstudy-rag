"""M3 datasets and normalized provenance links. Completed datasets are immutable."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDTimestampMixin


class ChunkRun(UUIDTimestampMixin, Base):
    __tablename__ = "chunk_runs"
    __table_args__ = (
        UniqueConstraint(
            "id", "parse_run_id", "tenant_id", name="uq_chunk_runs_id_parse_run_id_tenant_id"
        ),
        UniqueConstraint("id", "parse_run_id", name="uq_chunk_runs_id_parse_run_id"),
        UniqueConstraint(
            "ingestion_job_id", "generation", name="uq_chunk_runs_ingestion_job_id_generation"
        ),
        ForeignKeyConstraint(
            ["parse_run_id", "document_version_id", "tenant_id"],
            ["parse_runs.id", "parse_runs.document_version_id", "parse_runs.tenant_id"],
        ),
        Index(
            "uq_chunk_run_active_version",
            "document_version_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index(
            "uq_chunk_run_pending_version",
            "document_version_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING','RUNNING')"),
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','NEEDS_REVIEW','CANCELLED')",
            name="chunk_run_status",
        ),
        CheckConstraint(
            "NOT is_active OR (status = 'SUCCEEDED' AND validation_result IS NOT NULL "
            "AND validation_result IN ('PASS','PASS_WITH_WARNINGS'))",
            name="active_chunks_validated",
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"))
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID] = mapped_column(index=True)
    ingestion_job_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_jobs.id"), index=True)
    generation: Mapped[int]
    chunker_name: Mapped[str] = mapped_column(String(80))
    chunker_version: Mapped[str] = mapped_column(String(80))
    configuration_version: Mapped[str] = mapped_column(String(80))
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    tokenizer_name: Mapped[str] = mapped_column(String(100))
    tokenizer_version: Mapped[str] = mapped_column(String(100))
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    validation_result: Mapped[str | None] = mapped_column(String(24))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    force: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_ms: Mapped[int | None]
    worker_identity: Mapped[str | None] = mapped_column(String(120))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(300))
    correlation_id: Mapped[UUID]


class QuestionArtifact(UUIDTimestampMixin, Base):
    __tablename__ = "question_artifacts"
    __table_args__ = (
        UniqueConstraint("id", "chunk_run_id", name="uq_question_artifacts_id_chunk_run_id"),
        UniqueConstraint(
            "chunk_run_id", "question_hash", name="uq_question_artifacts_chunk_run_id_question_hash"
        ),
        ForeignKeyConstraint(
            ["chunk_run_id", "parse_run_id", "tenant_id"],
            ["chunk_runs.id", "chunk_runs.parse_run_id", "chunk_runs.tenant_id"],
        ),
        CheckConstraint("NOT answer_inferred", name="never_infer_answers"),
    )
    chunk_run_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID]
    tenant_id: Mapped[UUID]
    question_hash: Mapped[str] = mapped_column(String(64))
    question_number: Mapped[str | None] = mapped_column(String(30))
    question_text: Mapped[str] = mapped_column(Text)
    question_type: Mapped[str] = mapped_column(String(30))
    explicit_answer: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    extraction_status: Mapped[str] = mapped_column(String(40))
    structure_inferred: Mapped[bool] = mapped_column(Boolean)
    answer_inferred: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    authority: Mapped[dict[str, Any]] = mapped_column(JSONB)
    page_start: Mapped[int | None]
    page_end: Mapped[int | None]


class QuestionOption(UUIDTimestampMixin, Base):
    __tablename__ = "question_options"
    __table_args__ = (
        UniqueConstraint("question_id", "ordinal", name="uq_question_options_question_id_ordinal"),
        UniqueConstraint("question_id", "label", name="uq_question_options_question_id_label"),
        CheckConstraint("ordinal >= 0", name="option_order_nonnegative"),
    )
    question_id: Mapped[UUID] = mapped_column(ForeignKey("question_artifacts.id"), index=True)
    ordinal: Mapped[int]
    label: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text)


class Chunk(UUIDTimestampMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("id", "chunk_run_id", name="uq_chunks_id_chunk_run_id"),
        UniqueConstraint(
            "id", "chunk_run_id", "parse_run_id", name="uq_chunks_id_chunk_run_id_parse_run_id"
        ),
        UniqueConstraint(
            "chunk_run_id", "sequence_number", name="uq_chunks_chunk_run_id_sequence_number"
        ),
        UniqueConstraint("chunk_run_id", "chunk_hash", name="uq_chunks_chunk_run_id_chunk_hash"),
        ForeignKeyConstraint(
            ["chunk_run_id", "parse_run_id", "tenant_id"],
            ["chunk_runs.id", "chunk_runs.parse_run_id", "chunk_runs.tenant_id"],
        ),
        ForeignKeyConstraint(
            ["parent_chunk_id", "chunk_run_id"], ["chunks.id", "chunks.chunk_run_id"]
        ),
        ForeignKeyConstraint(
            ["question_id", "chunk_run_id"],
            ["question_artifacts.id", "question_artifacts.chunk_run_id"],
        ),
        CheckConstraint(
            "token_count >= 0 AND retrieval_token_count >= 0", name="chunk_tokens_nonnegative"
        ),
        CheckConstraint("page_start >= 1 AND page_end >= page_start", name="chunk_page_range"),
        CheckConstraint("parent_chunk_id IS NULL OR parent_chunk_id <> id", name="no_self_parent"),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    chunk_run_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID]
    parent_chunk_id: Mapped[UUID | None]
    question_id: Mapped[UUID | None]
    chunk_type: Mapped[str] = mapped_column(String(40), index=True)
    sequence_number: Mapped[int]
    raw_text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    retrieval_text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int]
    retrieval_token_count: Mapped[int]
    page_start: Mapped[int | None]
    page_end: Mapped[int | None]
    chunk_hash: Mapped[str] = mapped_column(String(64))
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ChunkSourceElement(UUIDTimestampMixin, Base):
    __tablename__ = "chunk_source_elements"
    __table_args__ = (
        UniqueConstraint("chunk_id", "position", name="uq_chunk_source_elements_chunk_id_position"),
        ForeignKeyConstraint(
            ["chunk_id", "chunk_run_id", "parse_run_id"],
            ["chunks.id", "chunks.chunk_run_id", "chunks.parse_run_id"],
        ),
        ForeignKeyConstraint(
            ["element_id", "parse_run_id"],
            ["document_elements.id", "document_elements.parse_run_id"],
        ),
        CheckConstraint(
            "start_offset >= 0 AND end_offset >= start_offset", name="source_offsets_valid"
        ),
    )
    chunk_id: Mapped[UUID] = mapped_column(index=True)
    chunk_run_id: Mapped[UUID]
    parse_run_id: Mapped[UUID]
    element_id: Mapped[UUID] = mapped_column(index=True)
    position: Mapped[int]
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    role: Mapped[str] = mapped_column(String(40))


class ChunkSourcePage(UUIDTimestampMixin, Base):
    __tablename__ = "chunk_source_pages"
    __table_args__ = (
        UniqueConstraint("chunk_id", "page_id", name="uq_chunk_source_pages_chunk_id_page_id"),
        ForeignKeyConstraint(
            ["chunk_id", "chunk_run_id", "parse_run_id"],
            ["chunks.id", "chunks.chunk_run_id", "chunks.parse_run_id"],
        ),
        ForeignKeyConstraint(
            ["page_id", "parse_run_id"], ["document_pages.id", "document_pages.parse_run_id"]
        ),
    )
    chunk_id: Mapped[UUID] = mapped_column(index=True)
    chunk_run_id: Mapped[UUID]
    parse_run_id: Mapped[UUID]
    page_id: Mapped[UUID]


class ChunkArtifactRelation(UUIDTimestampMixin, Base):
    __tablename__ = "chunk_artifact_relations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_id", "chunk_run_id", "parse_run_id"],
            ["chunks.id", "chunks.chunk_run_id", "chunks.parse_run_id"],
        ),
        ForeignKeyConstraint(
            ["table_id", "parse_run_id"], ["table_artifacts.id", "table_artifacts.parse_run_id"]
        ),
        ForeignKeyConstraint(
            ["figure_id", "parse_run_id"], ["figure_artifacts.id", "figure_artifacts.parse_run_id"]
        ),
        ForeignKeyConstraint(
            ["formula_id", "parse_run_id"],
            ["formula_artifacts.id", "formula_artifacts.parse_run_id"],
        ),
        CheckConstraint(
            "num_nonnulls(table_id,figure_id,formula_id) = 1", name="exactly_one_source_artifact"
        ),
    )
    chunk_id: Mapped[UUID] = mapped_column(index=True)
    chunk_run_id: Mapped[UUID]
    parse_run_id: Mapped[UUID]
    table_id: Mapped[UUID | None]
    figure_id: Mapped[UUID | None]
    formula_id: Mapped[UUID | None]


class ChunkRelation(UUIDTimestampMixin, Base):
    __tablename__ = "chunk_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "target_id",
            "relation",
            name="uq_chunk_relations_source_id_target_id_relation",
        ),
        ForeignKeyConstraint(["source_id", "chunk_run_id"], ["chunks.id", "chunks.chunk_run_id"]),
        ForeignKeyConstraint(["target_id", "chunk_run_id"], ["chunks.id", "chunks.chunk_run_id"]),
        CheckConstraint("source_id <> target_id", name="chunk_relation_not_self"),
    )
    chunk_run_id: Mapped[UUID]
    source_id: Mapped[UUID] = mapped_column(index=True)
    target_id: Mapped[UUID]
    relation: Mapped[str] = mapped_column(String(40))


class ChunkValidationFinding(UUIDTimestampMixin, Base):
    __tablename__ = "chunk_validation_findings"
    __table_args__ = (
        ForeignKeyConstraint(["chunk_id", "chunk_run_id"], ["chunks.id", "chunks.chunk_run_id"]),
    )
    chunk_run_id: Mapped[UUID] = mapped_column(ForeignKey("chunk_runs.id"), index=True)
    chunk_id: Mapped[UUID | None]
    severity: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
