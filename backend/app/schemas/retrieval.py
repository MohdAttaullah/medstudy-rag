"""M5 wire contracts.

Two things are deliberately absent from every response here and must stay absent: a generated
answer, and any number presented as confidence, sufficiency or medical correctness. What a client
receives is a ranked list of **evidence candidates** with their provenance, plus the diagnostics
that explain why they were ranked that way.

Dense vectors are also absent. A 768-number array is of no use to a browser and would put the
corpus representation on the wire for no benefit.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.retrieval_config import SparseAnalyzerConfig


class QueryEncoderVersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    model_id: str
    model_revision: str
    tokenizer_revision: str
    model_checksum: str
    tokenizer_checksum: str
    embedding_dimension: int
    pooling_strategy: str
    normalization: str
    distance_metric: str
    max_query_tokens: int
    dtype: str
    normalization_version: str
    configuration_version: str
    semantics_fingerprint: str
    library_versions: dict[str, Any] = Field(default_factory=dict)


class SparseIndexVersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    analyzer_name: str
    analyzer_version: str
    unicode_normalization: str
    case_policy: str
    compound_policy: str
    stopword_policy: str
    stopword_count: int
    min_term_length: int
    max_term_length: int
    expansion: str
    configuration_version: str
    analyzer_fingerprint: str


class SparseIndexView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_version_id: UUID
    chunk_run_id: UUID
    sparse_index_version_id: UUID
    status: str
    is_active: bool
    expected_chunk_count: int
    indexed_chunk_count: int
    verified_chunk_count: int
    term_count: int
    posting_count: int
    total_length: int
    corpus_fingerprint: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    started_at: Any = None
    completed_at: Any = None
    activated_at: Any = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    correlation_id: UUID


class SparseFindingView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sparse_index_id: UUID
    chunk_id: UUID | None = None
    severity: str
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SparseIndexSummaryView(BaseModel):
    """Lexical state of one document version, and whether it can take part in retrieval."""

    document_version_id: UUID
    ingestion_status: str
    sparse_indexes: int = 0
    sparse_index: SparseIndexView | None = None
    sparse_index_version: SparseIndexVersionView | None = None
    finding_counts: dict[str, int] = Field(default_factory=dict)
    chunk_types: dict[str, int] = Field(default_factory=dict)
    # True only when the dense and lexical lanes are both active, verified and built from the
    # same chunk dataset. It says the version is searchable, never that it is answerable.
    retrieval_ready: bool = False
    lanes_aligned: bool = False


class RetrievalStatusView(BaseModel):
    """What this tenant could search right now, without running a query."""

    tenant_id: UUID
    document_versions: int
    chunk_runs: int
    dense_index_runs: int
    sparse_indexes: int
    embedding_version_id: UUID | None = None
    sparse_index_version_id: UUID | None = None
    query_encoder: QueryEncoderVersionView | None = None
    analyzer: SparseAnalyzerConfig | None = None
    modes: list[str] = Field(default_factory=list)
    default_mode: str
    dense_top_k: int
    sparse_top_k: int
    final_top_k: int
    rrf_k: int
    bm25_k1: float
    bm25_b: float
    degradation_policy: str
    corpus_error: str | None = None
    # Stated explicitly in the payload so no client has to infer it from an absence.
    answering_enabled: Literal[False] = False


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[UUID] = Field(default_factory=list, max_length=50)
    document_version_ids: list[UUID] = Field(default_factory=list, max_length=50)
    chunk_types: list[str] = Field(default_factory=list, max_length=20)
    source_types: list[str] = Field(default_factory=list, max_length=20)
    authority_levels: list[str] = Field(default_factory=list, max_length=10)
    subjects: list[str] = Field(default_factory=list, max_length=20)
    specialties: list[str] = Field(default_factory=list, max_length=20)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No tenant field, and there never will be one: the tenant comes from the authenticated
    # principal, and accepting it from a request body is how cross-tenant reads happen.
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["DENSE_ONLY", "BM25_ONLY", "HYBRID_RRF"] | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    filters: SearchFilters | None = None


class ProvenanceView(BaseModel):
    document_id: UUID
    document_version_id: UUID
    chunk_run_id: UUID
    document_title: str
    source_type: str
    authority_level: str
    subject: str | None = None
    specialty: str | None = None
    chunk_type: str
    page_start: int | None = None
    page_end: int | None = None
    sequence_number: int
    parent_chunk_id: UUID | None = None
    question_id: UUID | None = None
    source_element_ids: list[UUID] = Field(default_factory=list)


class CandidateView(BaseModel):
    """One evidence candidate.

    Every score and rank here is a retrieval diagnostic describing how a lane ordered results.
    None of them is a confidence, a probability or a statement about medical correctness, and the
    dense and lexical scores are on unrelated scales and must not be compared with each other.
    """

    chunk_id: UUID
    fused_rank: int
    fused_score: float
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    lanes: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    preview: str
    provenance: ProvenanceView


class LaneHitView(BaseModel):
    chunk_id: UUID
    rank: int
    score: float
    chunk_type: str | None = None
    source_type: str | None = None
    authority_level: str | None = None
    matched_terms: list[str] = Field(default_factory=list)


class TraceView(BaseModel):
    """Everything needed to reproduce a retrieval. It carries a query hash, never the question."""

    correlation_id: UUID
    mode: str
    retrieval_config_version: str
    retrieval_config_fingerprint: str
    query_encoder_version_id: UUID | None = None
    query_encoder_fingerprint: str | None = None
    query_hash: str
    query_token_count: int | None = None
    normalization_version: str
    embedding_version_id: UUID | None = None
    sparse_index_version_id: UUID | None = None
    index_run_ids: list[UUID] = Field(default_factory=list)
    sparse_index_ids: list[UUID] = Field(default_factory=list)
    chunk_run_ids: list[UUID] = Field(default_factory=list)
    dense_top_k: int
    sparse_top_k: int
    final_top_k: int
    rrf_k: int
    dense_weight: float
    sparse_weight: float
    bm25_k1: float
    bm25_b: float
    dense_candidates: int
    sparse_candidates: int
    fused_candidates: int
    durations_ms: dict[str, float] = Field(default_factory=dict)
    query_vector_cached: bool = False


class SearchResponse(BaseModel):
    correlation_id: UUID
    mode: str
    # Named for what it is. This is not an answer and no field in this response is one.
    candidates: list[CandidateView] = Field(default_factory=list)
    dense: list[LaneHitView] = Field(default_factory=list)
    sparse: list[LaneHitView] = Field(default_factory=list)
    trace: TraceView
    warnings: list[str] = Field(default_factory=list)
    answering_enabled: Literal[False] = False


class SparseReindexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analyzer: SparseAnalyzerConfig | None = None
