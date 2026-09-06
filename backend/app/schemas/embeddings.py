"""Bounded, tenant-authorized M4 inspection contracts.

No route returns a raw dense vector. A 768-number array is not information an ordinary reader can
act on, it inflates every response, and it would let the index be reconstructed through the
document API. Vectors are described by dimension, norm and checksum instead.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.embedding_config import EmbeddingConfig
from app.schemas.documents import ORMView


class ReembedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False
    config: EmbeddingConfig | None = None


class EmbeddingVersionView(ORMView):
    id: UUID
    model_provider: str
    model_id: str
    model_revision: str
    tokenizer_revision: str
    model_checksum: str
    embedding_dimension: int
    pooling_strategy: str
    normalization: str
    distance_metric: str
    max_input_tokens: int
    dtype: str
    input_builder_version: str
    configuration_version: str
    semantics_fingerprint: str
    library_versions: dict[str, Any]
    created_at: datetime


class EmbeddingRunView(ORMView):
    id: UUID
    document_id: UUID
    document_version_id: UUID
    chunk_run_id: UUID
    embedding_version_id: UUID
    ingestion_job_id: UUID
    generation: int
    status: str
    is_active: bool
    eligible_chunk_count: int
    embedded_chunk_count: int
    reused_chunk_count: int
    failed_chunk_count: int
    skipped_chunk_count: int
    input_fingerprint: str | None
    policy_fingerprint: str
    metrics: dict[str, Any]
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    created_at: datetime


class ChunkEmbeddingView(ORMView):
    """Metadata about one vector. The dense array itself is deliberately not exposed."""

    id: UUID
    embedding_run_id: UUID
    chunk_id: UUID
    point_id: UUID
    vector_name: str
    input_hash: str
    vector_checksum: str
    dimension: int
    token_count: int
    vector_norm: float
    truncated: bool
    reused: bool


class IndexRunView(ORMView):
    id: UUID
    document_id: UUID
    document_version_id: UUID
    chunk_run_id: UUID
    embedding_run_id: UUID
    embedding_version_id: UUID
    attempt: int
    physical_collection: str
    alias: str
    vector_name: str
    schema_version: str
    status: str
    is_active: bool
    expected_point_count: int
    indexed_point_count: int
    verified_point_count: int
    upsert_batches: int
    policy_fingerprint: str
    metrics: dict[str, Any]
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    activated_at: datetime | None
    duration_ms: int | None
    created_at: datetime


class IndexFindingView(ORMView):
    id: UUID
    embedding_run_id: UUID | None
    index_run_id: UUID | None
    chunk_id: UUID | None
    severity: str
    code: str
    message: str
    details: dict[str, Any]


class IndexStatisticsView(BaseModel):
    """Live index facts for one run, read back from the vector database at request time."""

    index_run_id: UUID
    collection: str
    vector_name: str
    dimension: int
    distance_metric: str
    expected_points: int
    live_points: int
    alias: str
    alias_target: str | None
    reachable: bool


class EmbeddingSummaryView(BaseModel):
    """What Document Details shows for one version. Every value is measured, never estimated."""

    document_version_id: UUID
    ingestion_status: str
    embedding_run: EmbeddingRunView | None = None
    embedding_version: EmbeddingVersionView | None = None
    index_run: IndexRunView | None = None
    embedding_runs: int = 0
    finding_counts: dict[str, int] = Field(default_factory=dict)
    chunk_types: dict[str, int] = Field(default_factory=dict)
