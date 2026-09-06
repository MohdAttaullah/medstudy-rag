"""M5 lexical index and query-encoder metadata.

The dense lane keeps its float arrays in Qdrant and its expected set in PostgreSQL. The lexical
lane keeps *everything* in PostgreSQL, and that is a deliberate choice rather than an omission:

* inverse document frequency is then computed over exactly the corpus a query may see — one
  tenant's active, verified, version-aligned indexes — instead of over whatever else happens to
  share a collection, including other tenants and superseded runs;
* the analyzer is ours and versioned, so a term is defined in one place for both the corpus and
  the query, and `HLA-B27` cannot mean one thing at index time and another at query time;
* term frequencies and document lengths are stored raw, so `k1` and `b` are runtime-safe and can
  be benchmarked without rebuilding anything;
* the same reconciliation discipline as M4 applies, because the expected set is durable and
  auditable rather than implied by a search engine's internal state.

Chunk identity is shared with the dense lane. There is no separate lexical document id, so a
candidate from either lane resolves through the same chunk to the same source element and page.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDTimestampMixin


class QueryEncoderVersion(UUIDTimestampMixin, Base):
    """What a query vector means: model, revision, pooling, dimension, metric, normalization.

    Separate from `embedding_versions` because it is a different checkpoint with a different
    tokenizer and a different input length. Recording it is what lets a retrieval report say which
    encoder produced a measured number, and what lets the service refuse to serve a query when the
    loaded encoder no longer matches the pinned revision.
    """

    __tablename__ = "query_encoder_versions"
    __table_args__ = (
        UniqueConstraint("semantics_fingerprint", name="uq_query_encoder_versions_semantics"),
        CheckConstraint("embedding_dimension > 0", name="query_encoder_dimension_positive"),
        CheckConstraint("max_query_tokens > 0", name="query_encoder_max_tokens_positive"),
        CheckConstraint(
            "distance_metric IN ('DOT','COSINE','EUCLID')", name="query_encoder_distance_metric"
        ),
    )

    model_provider: Mapped[str] = mapped_column(String(40))
    model_id: Mapped[str] = mapped_column(String(200), index=True)
    model_revision: Mapped[str] = mapped_column(String(80))
    tokenizer_revision: Mapped[str] = mapped_column(String(80))
    model_checksum: Mapped[str] = mapped_column(String(64))
    tokenizer_checksum: Mapped[str] = mapped_column(String(64))
    embedding_dimension: Mapped[int]
    pooling_strategy: Mapped[str] = mapped_column(String(24))
    normalization: Mapped[str] = mapped_column(String(24))
    distance_metric: Mapped[str] = mapped_column(String(16))
    max_query_tokens: Mapped[int]
    dtype: Mapped[str] = mapped_column(String(16))
    normalization_version: Mapped[str] = mapped_column(String(80))
    configuration_version: Mapped[str] = mapped_column(String(80))
    semantics_fingerprint: Mapped[str] = mapped_column(String(64))
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    library_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class SparseIndexVersion(UUIDTimestampMixin, Base):
    """What a lexical term means: the analyzer policy, and nothing else.

    Deliberately excludes `k1` and `b`. Those are scoring parameters applied at query time to
    stored raw frequencies, so they are runtime-safe and versioning them here would force a
    pointless rebuild every time a benchmark tried a different saturation.
    """

    __tablename__ = "sparse_index_versions"
    __table_args__ = (
        UniqueConstraint("analyzer_fingerprint", name="uq_sparse_index_versions_analyzer"),
        CheckConstraint(
            "min_term_length >= 1 AND max_term_length >= min_term_length",
            name="sparse_term_length_bounds",
        ),
        CheckConstraint(
            "case_policy IN ('NORMALIZED','NORMALIZED_AND_EXACT')", name="sparse_case_policy"
        ),
        CheckConstraint(
            "compound_policy IN ('WHOLE','WHOLE_AND_PARTS')", name="sparse_compound_policy"
        ),
        # No synonym or abbreviation expansion exists, and none may be introduced without a
        # deliberate migration: an uncontrolled expansion changes what the user asked.
        CheckConstraint("expansion = 'NONE'", name="sparse_no_uncontrolled_expansion"),
    )

    analyzer_name: Mapped[str] = mapped_column(String(80), index=True)
    analyzer_version: Mapped[str] = mapped_column(String(40))
    unicode_normalization: Mapped[str] = mapped_column(String(16))
    case_policy: Mapped[str] = mapped_column(String(30))
    compound_policy: Mapped[str] = mapped_column(String(30))
    stopword_policy: Mapped[str] = mapped_column(String(30))
    stopword_count: Mapped[int] = mapped_column(default=0, server_default="0")
    min_term_length: Mapped[int]
    max_term_length: Mapped[int]
    expansion: Mapped[str] = mapped_column(String(20))
    configuration_version: Mapped[str] = mapped_column(String(80))
    analyzer_fingerprint: Mapped[str] = mapped_column(String(64))
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)


class SparseIndex(UUIDTimestampMixin, Base):
    """One durable attempt to build the lexical postings for one chunk dataset.

    Mirrors `index_runs`: staged, reconciled, then activated as a PostgreSQL state change. Only a
    VERIFIED, active index is ever searched, and it carries `chunk_run_id` so the retrieval
    service can prove it was built from the same chunk dataset as the dense index it is fused with.
    """

    __tablename__ = "sparse_indexes"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_sparse_indexes_id_tenant"),
        UniqueConstraint("ingestion_job_id", "generation", name="uq_sparse_indexes_job_generation"),
        ForeignKeyConstraint(
            ["chunk_run_id", "document_version_id", "tenant_id"],
            ["chunk_runs.id", "chunk_runs.document_version_id", "chunk_runs.tenant_id"],
        ),
        Index(
            "uq_sparse_index_active_version",
            "document_version_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index(
            "uq_sparse_index_pending_version",
            "document_version_id",
            unique=True,
            postgresql_where=text("status IN ('STAGING','VERIFYING')"),
        ),
        CheckConstraint(
            "status IN ('STAGING','VERIFYING','VERIFIED','FAILED','CANCELLED','SUPERSEDED')",
            name="sparse_index_status",
        ),
        # Only a fully reconciled index may be active. This is the guarantee the retrieval
        # service relies on when it decides which lexical index is safe to search.
        CheckConstraint(
            "NOT is_active OR (status = 'VERIFIED' "
            "AND verified_chunk_count = expected_chunk_count "
            "AND indexed_chunk_count = expected_chunk_count)",
            name="active_sparse_index_verified",
        ),
        CheckConstraint(
            "expected_chunk_count >= 0 AND indexed_chunk_count >= 0 "
            "AND verified_chunk_count >= 0 AND posting_count >= 0 AND term_count >= 0 "
            "AND total_length >= 0",
            name="sparse_counts_nonnegative",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"))
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    chunk_run_id: Mapped[UUID] = mapped_column(index=True)
    sparse_index_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("sparse_index_versions.id"), index=True
    )
    ingestion_job_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_jobs.id"), index=True)
    generation: Mapped[int]
    schema_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(24), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    expected_chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    indexed_chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    verified_chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    term_count: Mapped[int] = mapped_column(default=0, server_default="0")
    posting_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    total_length: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    corpus_fingerprint: Mapped[str | None] = mapped_column(String(64))
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_ms: Mapped[int | None]
    worker_identity: Mapped[str | None] = mapped_column(String(120))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(300))
    correlation_id: Mapped[UUID]


class SparseDocument(UUIDTimestampMixin, Base):
    """One indexed chunk in one lexical index.

    Holds the document length BM25 normalizes by, and the facets the lexical lane filters on so a
    filtered search never has to read chunk bodies to decide what it is allowed to score.
    """

    __tablename__ = "sparse_documents"
    __table_args__ = (
        UniqueConstraint("sparse_index_id", "chunk_id", name="uq_sparse_documents_index_chunk"),
        ForeignKeyConstraint(
            ["sparse_index_id", "tenant_id"], ["sparse_indexes.id", "sparse_indexes.tenant_id"]
        ),
        ForeignKeyConstraint(["chunk_id", "chunk_run_id"], ["chunks.id", "chunks.chunk_run_id"]),
        CheckConstraint("length >= 0 AND distinct_terms >= 0", name="sparse_document_lengths"),
        Index("ix_sparse_documents_scope", "sparse_index_id", "tenant_id"),
    )

    sparse_index_id: Mapped[UUID] = mapped_column(index=True)
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_id: Mapped[UUID]
    document_version_id: Mapped[UUID]
    chunk_run_id: Mapped[UUID]
    chunk_id: Mapped[UUID] = mapped_column(index=True)
    chunk_type: Mapped[str] = mapped_column(String(40))
    # Assessment material stays distinguishable from reference evidence in the lexical lane too.
    # Being indexed never promotes a question key to authority.
    source_type: Mapped[str] = mapped_column(String(40))
    authority_level: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str | None] = mapped_column(String(120))
    specialty: Mapped[str | None] = mapped_column(String(120))
    length: Mapped[int]
    distinct_terms: Mapped[int]


class SparsePosting(Base):
    """One (term, chunk) occurrence with its raw term frequency.

    Raw, not pre-weighted. Storing a finished BM25 weight would bake the corpus statistics and the
    saturation parameters into the index, and every parameter experiment would then need a rebuild
    and would silently compare against differently-weighted history.
    """

    __tablename__ = "sparse_postings"
    __table_args__ = (
        UniqueConstraint("sparse_index_id", "chunk_id", "term", name="uq_sparse_postings_identity"),
        ForeignKeyConstraint(["sparse_index_id"], ["sparse_indexes.id"]),
        CheckConstraint("term_frequency > 0", name="sparse_posting_frequency_positive"),
        # The lookup every query makes: the terms of one question across the active indexes.
        Index("ix_sparse_postings_term", "term", "sparse_index_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sparse_index_id: Mapped[UUID] = mapped_column(index=True)
    chunk_id: Mapped[UUID]
    term: Mapped[str] = mapped_column(String(80))
    term_frequency: Mapped[int]


class SparseTerm(Base):
    """Per-index document frequency, so query-time inverse document frequency is a cheap sum.

    Document frequencies are additive across indexes because a chunk belongs to exactly one
    sparse index, so summing these rows over the active set gives the exact corpus-level statistic
    for the slice being searched, with no approximation and no cross-tenant contamination.
    """

    __tablename__ = "sparse_terms"
    __table_args__ = (
        UniqueConstraint("sparse_index_id", "term", name="uq_sparse_terms_identity"),
        ForeignKeyConstraint(["sparse_index_id"], ["sparse_indexes.id"]),
        CheckConstraint(
            "document_frequency > 0 AND total_frequency >= document_frequency",
            name="sparse_term_frequencies_positive",
        ),
        Index("ix_sparse_terms_term", "term", "sparse_index_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sparse_index_id: Mapped[UUID] = mapped_column(index=True)
    term: Mapped[str] = mapped_column(String(80))
    document_frequency: Mapped[int]
    total_frequency: Mapped[int]


class SparseValidationFinding(UUIDTimestampMixin, Base):
    """Append-only structural findings about lexical index reconciliation.

    These describe whether the postings agree with the chunks they claim to represent. None of
    them is a measure of retrieval relevance or of medical correctness, and none should ever be
    reported as one. Retrieval quality is measured separately, against a gold query set.
    """

    __tablename__ = "sparse_validation_findings"
    __table_args__ = (
        ForeignKeyConstraint(["sparse_index_id"], ["sparse_indexes.id"]),
        CheckConstraint(
            "severity IN ('INFO','WARNING','ERROR','CRITICAL')", name="sparse_finding_severity"
        ),
    )

    sparse_index_id: Mapped[UUID] = mapped_column(index=True)
    chunk_id: Mapped[UUID | None]
    severity: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
