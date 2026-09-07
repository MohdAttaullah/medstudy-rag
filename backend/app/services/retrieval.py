"""Hybrid retrieval orchestration.

    question
       |
       +-----------------------------+
       |                             |
   query encoder                 BM25 analyzer
       |                             |
   dense top-k                  lexical top-k
       |                             |
       +-------------+---------------+
                     |
                    RRF
                     |
              provenance hydration
                     |
             candidate set (evidence candidates only)

The order of operations is not incidental. The corpus is resolved *first*, from PostgreSQL, and
every later step is confined to it. The query is encoded only after the corpus is known to exist,
so an unusable corpus never causes a model forward pass. Hydration happens last and reads the
canonical store, so what a caller finally sees is the document, not the index's opinion of it.

This service produces **evidence candidates**. It does not summarise, rank by authority, judge
sufficiency, or generate anything. No provider SDK is imported anywhere on this path.
"""

import logging
import math
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import DomainError
from app.core.retrieval_config import (
    QueryEncoderConfig,
    RetrievalConfig,
    SparseAnalyzerConfig,
)
from app.models.embeddings import EmbeddingVersion
from app.models.retrieval import QueryEncoderVersion, SparseIndexVersion
from app.observability.retrieval import RetrievalMetrics
from app.repositories.retrieval import PostgresSparseRetriever, hydrate, resolve_corpus
from app.retrieval.dense.search import DenseSearch
from app.retrieval.errors import RetrievalError
from app.retrieval.fusion.rrf import ReciprocalRankFusion
from app.retrieval.model import (
    CandidateSet,
    QueryEncoderSpec,
    QueryVector,
    Ranking,
    RetrievalCorpus,
    RetrievalFilters,
    RetrievalHit,
    RetrievalTrace,
)
from app.security.auth import Principal

logger = logging.getLogger("medical_rag.retrieval")

MODES = ("DENSE_ONLY", "BM25_ONLY", "HYBRID_RRF")


class RetrievalService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        encoder_config: QueryEncoderConfig,
        analyzer: SparseAnalyzerConfig,
        config: RetrievalConfig,
        encoder_factory: Any = None,
        index_factory: Any = None,
        metrics: RetrievalMetrics | None = None,
    ) -> None:
        self.sessions = sessions
        self.encoder_config = encoder_config
        self.analyzer = analyzer
        self.config = config
        self._encoder_factory = encoder_factory
        self._index_factory = index_factory
        self.metrics = metrics
        self._encoder: Any = None

    # ------------------------------------------------------------------ dependencies

    def encoder(self) -> Any:
        if self._encoder is None:
            if self._encoder_factory is None:
                raise RetrievalError("QUERY_ENCODER_UNAVAILABLE")
            self._encoder = self._encoder_factory()
        return self._encoder

    def index(self) -> Any:
        if self._index_factory is None:
            raise RetrievalError("DENSE_INDEX_NOT_READY")
        built = self._index_factory()
        if built is None:
            raise RetrievalError("DENSE_INDEX_NOT_READY")
        return built

    # ------------------------------------------------------------------ encoder identity

    def _verify_specification(self, spec: QueryEncoderSpec) -> None:
        """The loaded encoder must be the pinned one. No substitution, ever."""
        if (
            spec.model_id != self.encoder_config.model_id
            or spec.model_revision != self.encoder_config.model_revision
            or spec.tokenizer_revision != self.encoder_config.tokenizer_revision
        ):
            raise RetrievalError(
                "QUERY_ENCODER_REVISION_MISMATCH",
                {"expected": self.encoder_config.model_revision, "loaded": spec.model_revision},
            )
        if self.encoder_config.verify_model_checksum and (
            spec.model_checksum != self.encoder_config.model_checksum
            or spec.tokenizer_checksum != self.encoder_config.tokenizer_checksum
        ):
            raise RetrievalError("QUERY_ENCODER_CHECKSUM_MISMATCH")

    def encoder_version(self, session: Session, spec: QueryEncoderSpec) -> QueryEncoderVersion:
        """Find or create the durable identity of the query vector space."""
        existing = session.scalar(
            select(QueryEncoderVersion).where(
                QueryEncoderVersion.semantics_fingerprint == spec.semantics_fingerprint
            )
        )
        if existing is not None:
            return existing
        statement = insert(QueryEncoderVersion).values(
            id=uuid4(),
            model_provider=spec.provider,
            model_id=spec.model_id,
            model_revision=spec.model_revision,
            tokenizer_revision=spec.tokenizer_revision,
            model_checksum=spec.model_checksum,
            tokenizer_checksum=spec.tokenizer_checksum,
            embedding_dimension=spec.dimension,
            pooling_strategy=spec.pooling,
            normalization=spec.normalization,
            distance_metric=spec.distance_metric,
            max_query_tokens=spec.max_query_tokens,
            dtype=spec.dtype,
            normalization_version=spec.normalization_version,
            configuration_version=self.encoder_config.version,
            semantics_fingerprint=spec.semantics_fingerprint,
            policy_fingerprint=self.encoder_config.fingerprint,
            config_snapshot=self.encoder_config.model_dump(mode="json"),
            library_versions=dict(spec.library_versions),
        )
        session.execute(statement.on_conflict_do_nothing(index_elements=["semantics_fingerprint"]))
        version = session.scalar(
            select(QueryEncoderVersion).where(
                QueryEncoderVersion.semantics_fingerprint == spec.semantics_fingerprint
            )
        )
        assert version is not None
        return version

    def _verify_vector_space(
        self, session: Session, spec: QueryEncoderSpec, corpus: RetrievalCorpus
    ) -> None:
        """Query vectors and stored article vectors must occupy the same space.

        A dot product between a 768-dimension CLS vector and something produced by a different
        pooling or a normalized model is still a number. It is simply not a similarity, and the
        ranking built from it would look entirely normal while being meaningless.
        """
        if corpus.embedding_version_id is None:
            raise RetrievalError("DENSE_INDEX_NOT_READY")
        stored = session.get(EmbeddingVersion, corpus.embedding_version_id)
        if stored is None:
            raise RetrievalError("DENSE_INDEX_NOT_READY")
        divergent: dict[str, object] = {
            name: {"query": query, "article": article}
            for name, query, article in (
                ("embedding_dimension", spec.dimension, stored.embedding_dimension),
                ("pooling_strategy", spec.pooling, stored.pooling_strategy),
                ("normalization", spec.normalization, stored.normalization),
                ("distance_metric", spec.distance_metric, stored.distance_metric),
                ("dtype", spec.dtype, stored.dtype),
            )
            if query != article
        }
        if divergent:
            raise RetrievalError("RETRIEVAL_VECTOR_SPACE_MISMATCH", divergent)

    # ------------------------------------------------------------------ search

    def search(
        self,
        actor: Principal,
        query: str,
        correlation_id: UUID,
        *,
        mode: str | None = None,
        top_k: int | None = None,
        filters: RetrievalFilters | None = None,
    ) -> CandidateSet:
        actor.require_any("retrieval:search", "ask:submit")
        chosen = (mode or self.config.mode).upper()
        if chosen not in MODES:
            raise DomainError("INVALID_REQUEST", "Unknown retrieval mode.", 422)
        limit = min(top_k or self.config.final_top_k, self.config.final_top_k)
        started = time.perf_counter()
        try:
            result = self._search(actor, query, correlation_id, chosen, limit, filters)
        except DomainError as exc:
            if self.metrics is not None:
                self.metrics.failures.labels(mode=chosen, code=exc.code).inc()
            logger.info(
                "retrieval_failed",
                extra={
                    "event": "retrieval_failed",
                    "request_id": str(correlation_id),
                    "mode": chosen,
                    "code": exc.code,
                },
            )
            raise
        if self.metrics is not None:
            self.metrics.queries.labels(mode=chosen).inc()
            self.metrics.total.labels(mode=chosen).observe(time.perf_counter() - started)
        return result

    def _search(
        self,
        actor: Principal,
        query: str,
        correlation_id: UUID,
        mode: str,
        limit: int,
        filters: RetrievalFilters | None,
    ) -> CandidateSet:
        durations: dict[str, float] = {}
        warnings: list[str] = []
        started = time.perf_counter()

        with self.sessions() as session:
            corpus = resolve_corpus(session, actor.tenant_id)
            if corpus.empty:
                raise RetrievalError("RETRIEVAL_CORPUS_EMPTY")
            analyzer = self._analyzer_for(session, corpus)
        durations["corpus_ms"] = (time.perf_counter() - started) * 1000

        vector: QueryVector | None = None
        encoder_version_id: UUID | None = None
        encoder_fingerprint: str | None = None
        if mode in {"DENSE_ONLY", "HYBRID_RRF"}:
            encoding_started = time.perf_counter()
            vector, encoder_version_id, encoder_fingerprint = self._encode(query, corpus)
            durations["encoding_ms"] = (time.perf_counter() - encoding_started) * 1000

        dense: Ranking | None = None
        sparse: Ranking | None = None
        if mode in {"DENSE_ONLY", "HYBRID_RRF"} and vector is not None:
            lane = DenseSearch(
                self.index(),
                self.config,
                dimension=len(vector.values),
                metric=self.encoder_config.distance_metric,
            )
            dense = lane.search(
                vector.values,
                corpus=corpus,
                top_k=self.config.dense_top_k,
                filters=filters,
            )
            durations["dense_ms"] = dense.duration_ms

        if mode in {"BM25_ONLY", "HYBRID_RRF"}:
            try:
                with self.sessions() as session:
                    sparse = PostgresSparseRetriever(session, analyzer, self.config).search(
                        query,
                        corpus=corpus,
                        top_k=self.config.sparse_top_k,
                        filters=filters,
                    )
                durations["sparse_ms"] = sparse.duration_ms
            except RetrievalError:
                # Accuracy-first: the configured strategy either runs or the request fails. A
                # hybrid query that quietly became dense-only would return a differently-shaped
                # candidate set under the same name, and nothing downstream would know.
                if mode != "HYBRID_RRF" or self.config.degradation_policy == "FAIL_CLOSED":
                    raise
                warnings.append("SPARSE_LANE_UNAVAILABLE_DEGRADED_TO_DENSE")

        fusion_started = time.perf_counter()
        rankings = [ranking for ranking in (dense, sparse) if ranking is not None]
        if not rankings:
            raise RetrievalError("RETRIEVAL_MODE_UNAVAILABLE", {"mode": mode})
        fused = ReciprocalRankFusion(self.config.rrf_k).fuse(rankings, limit)
        durations["fusion_ms"] = (time.perf_counter() - fusion_started) * 1000

        hydration_started = time.perf_counter()
        with self.sessions() as session:
            if resolve_corpus(session, actor.tenant_id) != corpus:
                raise RetrievalError("RETRIEVAL_CORPUS_MISALIGNED")
            resolved = hydrate(
                session,
                actor.tenant_id,
                tuple(hit.chunk_id for hit in fused),
                corpus,
                self.config.preview_characters,
            )
        durations["hydration_ms"] = (time.perf_counter() - hydration_started) * 1000

        matched = {
            hit.chunk_id: hit.matched_terms for hit in (sparse.hits if sparse is not None else ())
        }
        candidates = []
        position = 0
        for hit in fused:
            resolution = resolved.get(hit.chunk_id)
            if resolution is None:
                # A candidate that cannot be resolved to a source is not evidence. Dropping it is
                # the only safe option, and it is reported rather than hidden.
                warnings.append("CANDIDATE_WITHOUT_PROVENANCE_DROPPED")
                continue
            provenance, preview = resolution
            position += 1
            candidates.append(
                RetrievalHit(
                    chunk_id=hit.chunk_id,
                    fused_rank=position,
                    fused_score=hit.fused_score,
                    dense_rank=hit.dense_rank,
                    dense_score=hit.dense_score,
                    sparse_rank=hit.sparse_rank,
                    sparse_score=hit.sparse_score,
                    lanes=hit.lanes,
                    provenance=provenance,
                    preview=preview,
                    matched_terms=matched.get(hit.chunk_id, ()),
                )
            )

        durations["total_ms"] = (time.perf_counter() - started) * 1000
        self._observe(durations, dense, sparse, len(candidates))
        trace = RetrievalTrace(
            correlation_id=correlation_id,
            mode=mode,
            retrieval_config_version=self.config.version,
            retrieval_config_fingerprint=self.config.fingerprint,
            query_encoder_version_id=encoder_version_id,
            query_encoder_fingerprint=encoder_fingerprint,
            query_hash=vector.query_hash if vector else _lexical_hash(query, analyzer),
            query_token_count=vector.token_count if vector else None,
            normalization_version=self.encoder_config.normalization_version,
            embedding_version_id=corpus.embedding_version_id,
            sparse_index_version_id=corpus.sparse_index_version_id,
            index_run_ids=corpus.index_run_ids,
            sparse_index_ids=corpus.sparse_index_ids,
            chunk_run_ids=corpus.chunk_run_ids,
            dense_top_k=self.config.dense_top_k,
            sparse_top_k=self.config.sparse_top_k,
            final_top_k=limit,
            rrf_k=self.config.rrf_k,
            dense_weight=self.config.dense_weight,
            sparse_weight=self.config.sparse_weight,
            bm25_k1=self.config.bm25_k1,
            bm25_b=self.config.bm25_b,
            dense_candidates=len(dense.hits) if dense else 0,
            sparse_candidates=len(sparse.hits) if sparse else 0,
            fused_candidates=len(candidates),
            durations_ms=durations,
            query_vector_cached=bool(vector and vector.cached),
        )
        # The question itself is never logged. A hash, the counts and the timings are enough to
        # correlate, reproduce and debug a retrieval without accumulating medical queries.
        logger.info(
            "retrieval_complete",
            extra={
                "event": "retrieval_complete",
                "request_id": str(correlation_id),
                "mode": mode,
                "query_hash": trace.query_hash,
                "dense_candidates": trace.dense_candidates,
                "sparse_candidates": trace.sparse_candidates,
                "fused_candidates": trace.fused_candidates,
                "duration_ms": durations["total_ms"],
            },
        )
        return CandidateSet(
            correlation_id=correlation_id,
            tenant_id=actor.tenant_id,
            mode=mode,
            candidates=tuple(candidates),
            dense=dense.hits if dense else (),
            sparse=sparse.hits if sparse else (),
            trace=trace,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    # ------------------------------------------------------------------ steps

    def _analyzer_for(self, session: Session, corpus: RetrievalCorpus) -> SparseAnalyzerConfig:
        """The analyzer the active lexical index was actually built with.

        Reading it back rather than assuming the configured one is what makes an analyzer change
        safe: a service running new configuration against an index built by the old analyzer would
        produce terms the postings do not contain, and would silently return nothing.
        """
        if corpus.sparse_index_version_id is None:
            raise RetrievalError("SPARSE_INDEX_NOT_READY")
        stored = session.get(SparseIndexVersion, corpus.sparse_index_version_id)
        if stored is None:
            raise RetrievalError("SPARSE_INDEX_NOT_READY")
        if stored.analyzer_fingerprint != self.analyzer.analyzer_fingerprint:
            raise RetrievalError(
                "SPARSE_INDEX_VERSION_MISMATCH",
                {
                    "configured": self.analyzer.analyzer_fingerprint[:12],
                    "indexed": stored.analyzer_fingerprint[:12],
                    "action": "rebuild the lexical index for the new analyzer version",
                },
            )
        return self.analyzer

    def _encode(
        self, query: str, corpus: RetrievalCorpus
    ) -> tuple[QueryVector, UUID | None, str | None]:
        started = time.perf_counter()
        encoder = self.encoder()
        vectors = encoder.encode_queries([query])
        if len(vectors) != 1:
            raise RetrievalError("QUERY_ENCODING_FAILED")
        if len(vectors[0].values) != self.encoder_config.embedding_dimension or not all(
            math.isfinite(value) for value in vectors[0].values
        ):
            raise RetrievalError("QUERY_VECTOR_INVALID")
        elapsed = time.perf_counter() - started
        if self.metrics is not None:
            self.metrics.encoding.observe(elapsed)
        spec = encoder.specification
        self._verify_specification(spec)
        with self.sessions.begin() as session:
            self._verify_vector_space(session, spec, corpus)
            version = self.encoder_version(session, spec)
            version_id = version.id
        return vectors[0], version_id, spec.semantics_fingerprint

    def _observe(
        self,
        durations: dict[str, float],
        dense: Ranking | None,
        sparse: Ranking | None,
        fused: int,
    ) -> None:
        if self.metrics is None:
            return
        if dense is not None:
            self.metrics.dense.observe(dense.duration_ms / 1000)
            self.metrics.dense_candidates.observe(len(dense.hits))
        if sparse is not None:
            self.metrics.sparse.observe(sparse.duration_ms / 1000)
            self.metrics.sparse_candidates.observe(len(sparse.hits))
        self.metrics.fusion.observe(durations.get("fusion_ms", 0.0) / 1000)
        self.metrics.hydration.observe(durations.get("hydration_ms", 0.0) / 1000)
        self.metrics.fused_candidates.observe(fused)


def _lexical_hash(query: str, analyzer: SparseAnalyzerConfig) -> str:
    """Query identity for a lexical-only retrieval, where no encoder produced one."""
    import hashlib

    from app.retrieval.sparse.analyzer import query_terms

    payload = "\x1f".join((analyzer.analyzer_fingerprint, *query_terms(query, analyzer)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
