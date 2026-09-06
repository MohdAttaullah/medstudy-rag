"""M4 embedding and vector-index metadata.

PostgreSQL is authoritative for *what should exist*; Qdrant holds the dense float arrays. Keeping
the mapping here is what makes reconciliation possible at all: without a durable expected set,
"the index looks fine" is an assertion about a system that cannot be audited.

Completed runs are historical records and are immutable.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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


class EmbeddingVersion(UUIDTimestampMixin, Base):
    """What a vector means: model, revision, pooling, dimension, metric, input representation.

    Deliberately not per ingestion run. Many EmbeddingRuns over many documents share one version;
    a change to any of these fields is a different vector space and must not be mixed into an
    existing collection.
    """

    __tablename__ = "embedding_versions"
    __table_args__ = (
        UniqueConstraint("semantics_fingerprint", name="uq_embedding_versions_semantics"),
        CheckConstraint("embedding_dimension > 0", name="embedding_dimension_positive"),
        CheckConstraint("max_input_tokens > 0", name="embedding_max_tokens_positive"),
        CheckConstraint(
            "distance_metric IN ('DOT','COSINE','EUCLID')", name="embedding_distance_metric"
        ),
    )

    model_provider: Mapped[str] = mapped_column(String(40))
    model_id: Mapped[str] = mapped_column(String(200), index=True)
    model_revision: Mapped[str] = mapped_column(String(80))
    tokenizer_revision: Mapped[str] = mapped_column(String(80))
    model_checksum: Mapped[str] = mapped_column(String(64))
    embedding_dimension: Mapped[int]
    pooling_strategy: Mapped[str] = mapped_column(String(24))
    normalization: Mapped[str] = mapped_column(String(24))
    distance_metric: Mapped[str] = mapped_column(String(16))
    max_input_tokens: Mapped[int]
    dtype: Mapped[str] = mapped_column(String(16))
    input_builder_version: Mapped[str] = mapped_column(String(80))
    configuration_version: Mapped[str] = mapped_column(String(80))
    semantics_fingerprint: Mapped[str] = mapped_column(String(64))
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    library_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class EmbeddingRun(UUIDTimestampMixin, Base):
    """One durable attempt to embed the retrieval-eligible chunks of one chunk run."""

    __tablename__ = "embedding_runs"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_embedding_runs_id_tenant"),
        UniqueConstraint(
            "id", "chunk_run_id", "tenant_id", name="uq_embedding_runs_id_chunk_run_tenant"
        ),
        UniqueConstraint("ingestion_job_id", "generation", name="uq_embedding_runs_job_generation"),
        ForeignKeyConstraint(
            ["chunk_run_id", "document_version_id", "tenant_id"],
            ["chunk_runs.id", "chunk_runs.document_version_id", "chunk_runs.tenant_id"],
        ),
        Index(
            "uq_embedding_run_active_version",
            "document_version_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index(
            "uq_embedding_run_pending_version",
            "document_version_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING','RUNNING')"),
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','NEEDS_REVIEW','CANCELLED')",
            name="embedding_run_status",
        ),
        # An active run must have succeeded and must have embedded every eligible chunk it found.
        CheckConstraint(
            "NOT is_active OR (status = 'SUCCEEDED' AND failed_chunk_count = 0 "
            "AND embedded_chunk_count = eligible_chunk_count)",
            name="active_embedding_run_complete",
        ),
        CheckConstraint(
            "eligible_chunk_count >= 0 AND embedded_chunk_count >= 0 AND failed_chunk_count >= 0",
            name="embedding_counts_nonnegative",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"))
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    chunk_run_id: Mapped[UUID] = mapped_column(index=True)
    embedding_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("embedding_versions.id"), index=True
    )
    ingestion_job_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_jobs.id"), index=True)
    generation: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    force: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    eligible_chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    embedded_chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    reused_chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    failed_chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    skipped_chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
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


class ChunkEmbedding(UUIDTimestampMixin, Base):
    """Metadata for one vector. The dense array itself lives in Qdrant.

    `input_hash` records exactly which embedding input produced this vector, which is what makes
    reuse safe: a vector is reused only when the same builder, model revision and text produced it,
    never because two chunks merely look alike.
    """

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "embedding_run_id", "chunk_id", "vector_name", name="uq_chunk_embedding_identity"
        ),
        UniqueConstraint("embedding_run_id", "point_id", name="uq_chunk_embedding_point"),
        ForeignKeyConstraint(
            ["embedding_run_id", "tenant_id"],
            ["embedding_runs.id", "embedding_runs.tenant_id"],
        ),
        ForeignKeyConstraint(["chunk_id", "chunk_run_id"], ["chunks.id", "chunks.chunk_run_id"]),
        CheckConstraint("dimension > 0", name="chunk_embedding_dimension_positive"),
        CheckConstraint("token_count >= 0", name="chunk_embedding_tokens_nonnegative"),
        # Truncated vectors must never be promoted: the policy is to reject, not to shorten.
        CheckConstraint("NOT truncated", name="chunk_embedding_never_truncated"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    embedding_run_id: Mapped[UUID] = mapped_column(index=True)
    chunk_run_id: Mapped[UUID]
    chunk_id: Mapped[UUID] = mapped_column(index=True)
    point_id: Mapped[UUID]
    vector_name: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    vector_checksum: Mapped[str] = mapped_column(String(64))
    dimension: Mapped[int]
    token_count: Mapped[int]
    vector_norm: Mapped[float] = mapped_column(Float)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    reused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class IndexRun(UUIDTimestampMixin, Base):
    """One durable attempt to load an embedding run's vectors into the index and verify them."""

    __tablename__ = "index_runs"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_index_runs_id_tenant"),
        UniqueConstraint("embedding_run_id", "attempt", name="uq_index_runs_embedding_run_attempt"),
        ForeignKeyConstraint(
            ["embedding_run_id", "tenant_id"],
            ["embedding_runs.id", "embedding_runs.tenant_id"],
        ),
        Index(
            "uq_index_run_active_version",
            "document_version_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "status IN ('STAGING','VERIFYING','VERIFIED','FAILED','CANCELLED','SUPERSEDED')",
            name="index_run_status",
        ),
        # Only a fully reconciled run may be active. This is the guarantee M5 relies on when it
        # decides which index run is safe to query.
        CheckConstraint(
            "NOT is_active OR (status = 'VERIFIED' "
            "AND verified_point_count = expected_point_count "
            "AND indexed_point_count = expected_point_count)",
            name="active_index_run_verified",
        ),
        CheckConstraint(
            "expected_point_count >= 0 AND indexed_point_count >= 0 AND verified_point_count >= 0",
            name="index_counts_nonnegative",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"))
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    chunk_run_id: Mapped[UUID]
    embedding_run_id: Mapped[UUID] = mapped_column(index=True)
    embedding_version_id: Mapped[UUID] = mapped_column(ForeignKey("embedding_versions.id"))
    attempt: Mapped[int] = mapped_column(default=0, server_default="0")
    physical_collection: Mapped[str] = mapped_column(String(120))
    alias: Mapped[str] = mapped_column(String(120))
    vector_name: Mapped[str] = mapped_column(String(40))
    schema_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(24), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    expected_point_count: Mapped[int] = mapped_column(default=0, server_default="0")
    indexed_point_count: Mapped[int] = mapped_column(default=0, server_default="0")
    verified_point_count: Mapped[int] = mapped_column(default=0, server_default="0")
    upsert_batches: Mapped[int] = mapped_column(default=0, server_default="0")
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None]
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(300))
    correlation_id: Mapped[UUID]


class IndexValidationFinding(UUIDTimestampMixin, Base):
    """Append-only structural findings about embeddings and index reconciliation.

    These describe vector and index integrity. None of them is a measure of retrieval relevance or
    of medical correctness, and none should ever be reported as one.
    """

    __tablename__ = "index_validation_findings"
    __table_args__ = (
        ForeignKeyConstraint(["embedding_run_id"], ["embedding_runs.id"]),
        ForeignKeyConstraint(["index_run_id"], ["index_runs.id"]),
        CheckConstraint(
            "severity IN ('INFO','WARNING','ERROR','CRITICAL')", name="index_finding_severity"
        ),
        CheckConstraint(
            "embedding_run_id IS NOT NULL OR index_run_id IS NOT NULL",
            name="index_finding_has_owner",
        ),
    )

    embedding_run_id: Mapped[UUID | None] = mapped_column(index=True)
    index_run_id: Mapped[UUID | None] = mapped_column(index=True)
    chunk_id: Mapped[UUID | None]
    severity: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    detail_text: Mapped[str | None] = mapped_column(Text)
