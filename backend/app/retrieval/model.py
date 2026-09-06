"""Provider-independent retrieval contracts.

Nothing here imports torch, transformers, qdrant_client or sqlalchemy. The orchestration service
depends only on these protocols, so the query encoder, the vector index and the lexical store are
each one module of work to replace, and the fusion stage cannot come to depend on either lane's
implementation.

Scores and ranks in these structures are **retrieval diagnostics**. They say how a lane ordered
its candidates. None of them is a confidence, a probability, or any statement about medical
correctness, and none may be presented as one.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class QueryEncoderSpec:
    """What a query vector produced by this encoder means, as reported by the loaded model."""

    provider: str
    model_id: str
    model_revision: str
    tokenizer_revision: str
    model_checksum: str
    tokenizer_checksum: str
    dimension: int
    pooling: str
    normalization: str
    distance_metric: str
    max_query_tokens: int
    dtype: str
    device: str
    normalization_version: str
    semantics_fingerprint: str
    library_versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueryVector:
    """One encoded query.

    `normalized` is the deterministic text that was actually encoded and `query_hash` covers it,
    so a trace can prove which text produced a ranking without recording the question itself.
    The original user text is kept by the caller and never rewritten here.
    """

    values: tuple[float, ...]
    token_count: int
    normalized: str
    query_hash: str
    checksum: str
    norm: float
    cached: bool = False


@dataclass(frozen=True, slots=True)
class QueryMeasurement:
    token_count: int
    exceeds_limit: bool


class QueryEncoder(Protocol):
    @property
    def specification(self) -> QueryEncoderSpec: ...

    def measure(self, queries: Sequence[str]) -> tuple[QueryMeasurement, ...]:
        """Token counts under the encoder's own tokenizer, without running inference."""
        ...

    def encode_queries(self, queries: Sequence[str]) -> tuple[QueryVector, ...]:
        """Query-side vectors, in input order. Never silently truncates an over-long query."""
        ...


@dataclass(frozen=True, slots=True)
class RetrievalCorpus:
    """The exact corpus slice a query is allowed to see, resolved from PostgreSQL.

    Both lanes are restricted to these identifiers. A document version appears here only when its
    dense index run and its sparse index are both active, both verified, and both built from the
    same chunk run, so a fused ranking can never mix candidates from different corpus versions.
    """

    tenant_id: UUID
    document_version_ids: tuple[UUID, ...]
    embedding_run_ids: tuple[UUID, ...]
    index_run_ids: tuple[UUID, ...]
    sparse_index_ids: tuple[UUID, ...]
    chunk_run_ids: tuple[UUID, ...]
    embedding_version_id: UUID | None
    sparse_index_version_id: UUID | None
    collection: str | None
    vector_name: str | None

    @property
    def empty(self) -> bool:
        return not self.document_version_ids


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """Optional query constraints. The tenant scope is not here because it is not optional."""

    document_ids: tuple[UUID, ...] = ()
    document_version_ids: tuple[UUID, ...] = ()
    chunk_types: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    authority_levels: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    specialties: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return any(
            (
                self.document_ids,
                self.document_version_ids,
                self.chunk_types,
                self.source_types,
                self.authority_levels,
                self.subjects,
                self.specialties,
            )
        )


@dataclass(frozen=True, slots=True)
class LaneHit:
    """One candidate as a single lane ranked it, before fusion and before hydration."""

    chunk_id: UUID
    score: float
    rank: int
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    chunk_type: str | None = None
    source_type: str | None = None
    authority_level: str | None = None
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Ranking:
    """One lane's ordered candidates plus what it cost to produce them."""

    lane: str
    hits: tuple[LaneHit, ...]
    duration_ms: float
    scanned: int = 0
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class FusedHit:
    chunk_id: UUID
    fused_score: float
    fused_rank: int
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    lanes: tuple[str, ...] = ()


class RankFusion(Protocol):
    def fuse(self, rankings: Sequence[Ranking], limit: int) -> tuple[FusedHit, ...]: ...


class DenseRetriever(Protocol):
    def search(
        self,
        vector: tuple[float, ...],
        *,
        corpus: RetrievalCorpus,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> Ranking: ...


class SparseRetriever(Protocol):
    def search(
        self,
        query: str,
        *,
        corpus: RetrievalCorpus,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> Ranking: ...


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a candidate came from, resolved from PostgreSQL rather than from the index payload."""

    document_id: UUID
    document_version_id: UUID
    chunk_run_id: UUID
    document_title: str
    source_type: str
    authority_level: str
    subject: str | None
    specialty: str | None
    chunk_type: str
    page_start: int | None
    page_end: int | None
    sequence_number: int
    parent_chunk_id: UUID | None
    question_id: UUID | None
    source_element_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """A fused candidate with its provenance hydrated from the canonical store.

    `dense_score` and `sparse_score` are the raw lane scores and are deliberately not comparable
    with each other; that incomparability is the reason fusion is rank-based.
    """

    chunk_id: UUID
    fused_rank: int
    fused_score: float
    dense_rank: int | None
    dense_score: float | None
    sparse_rank: int | None
    sparse_score: float | None
    lanes: tuple[str, ...]
    provenance: Provenance
    preview: str
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Everything needed to reproduce one retrieval, with no query text in it."""

    correlation_id: UUID
    mode: str
    retrieval_config_version: str
    retrieval_config_fingerprint: str
    query_encoder_version_id: UUID | None
    query_encoder_fingerprint: str | None
    query_hash: str
    query_token_count: int | None
    normalization_version: str
    embedding_version_id: UUID | None
    sparse_index_version_id: UUID | None
    index_run_ids: tuple[UUID, ...]
    sparse_index_ids: tuple[UUID, ...]
    chunk_run_ids: tuple[UUID, ...]
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
    durations_ms: dict[str, float]
    query_vector_cached: bool = False


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """The M5 deliverable and the exact input M6 reranking will consume.

    It is a set of evidence *candidates*. It is not an answer, not a summary, and not a statement
    that the corpus contains an answer at all.
    """

    correlation_id: UUID
    tenant_id: UUID
    mode: str
    candidates: tuple[RetrievalHit, ...]
    dense: tuple[LaneHit, ...]
    sparse: tuple[LaneHit, ...]
    trace: RetrievalTrace
    warnings: tuple[str, ...] = ()
