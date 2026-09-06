"""Dense retrieval over the verified vector index.

Two rules make this lane safe, and both are enforced here rather than in a caller.

*The corpus is resolved from PostgreSQL, never from the index.* Qdrant's `medrag_chunks_active`
alias is an operator convenience; it says which collection an operator most recently activated,
not which runs are verified, active and version-aligned for this tenant. The authoritative path is
tenant -> active verified IndexRun -> collection, and the search is restricted to exactly the
embedding runs that path yielded. Points from a staged, failed or superseded run remain in the
collection and are simply never selectable.

*Every filter is applied inside the index.* The tenant condition is not optional and cannot be
supplied by a client.
"""

import time
from typing import Any
from uuid import UUID

from app.core.retrieval_config import RetrievalConfig
from app.retrieval.errors import RetrievalError
from app.retrieval.model import LaneHit, Ranking, RetrievalCorpus, RetrievalFilters
from app.vectorindex.model import VectorSchema


class DenseSearch:
    """`DenseRetriever` backed by the M4 vector index."""

    def __init__(self, index: Any, config: RetrievalConfig, dimension: int, metric: str) -> None:
        self.index = index
        self.config = config
        self.dimension = dimension
        self.metric = metric

    def schema(self, corpus: RetrievalCorpus) -> VectorSchema:
        if not corpus.collection or not corpus.vector_name:
            raise RetrievalError("DENSE_INDEX_NOT_READY")
        return VectorSchema(
            collection=corpus.collection,
            vector_name=corpus.vector_name,
            dimension=self.dimension,
            distance=self.metric,
            payload_indexes=(),
            tenant_key="tenant_id",
        )

    def search(
        self,
        vector: tuple[float, ...],
        *,
        corpus: RetrievalCorpus,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> Ranking:
        if corpus.empty or not corpus.embedding_run_ids:
            raise RetrievalError("DENSE_INDEX_NOT_READY")
        schema = self.schema(corpus)
        selector: dict[str, Any] = {"tenant_id": str(corpus.tenant_id)}
        any_selector: dict[str, tuple[str, ...]] = {
            # Restricting to the active embedding runs is what excludes a superseded corpus
            # version, a failed load and a staged replacement in a single indexed condition.
            "embedding_run_id": tuple(str(value) for value in corpus.embedding_run_ids)
        }
        if filters is not None and filters.active:
            optional = (
                ("document_id", tuple(str(value) for value in filters.document_ids)),
                (
                    "document_version_id",
                    tuple(str(value) for value in filters.document_version_ids),
                ),
                ("chunk_type", filters.chunk_types),
                ("source_type", filters.source_types),
                ("authority_level", filters.authority_levels),
                ("subject", filters.subjects),
                ("specialty", filters.specialties),
            )
            for key, values in optional:
                if values:
                    any_selector[key] = tuple(values)

        started = time.perf_counter()
        points = self.index.search(
            schema,
            vector,
            limit=top_k,
            selector=selector,
            any_selector=any_selector,
        )
        hits = []
        for position, point in enumerate(points, start=1):
            chunk_id = point.payload.get("chunk_id")
            if not chunk_id:
                # A point without its chunk id cannot be traced back to a source, so it is not a
                # usable candidate. Dropping it is reported through the scanned/returned counts.
                continue
            hits.append(
                LaneHit(
                    chunk_id=UUID(str(chunk_id)),
                    score=point.score,
                    rank=position,
                    document_id=_uuid(point.payload.get("document_id")),
                    document_version_id=_uuid(point.payload.get("document_version_id")),
                    chunk_type=_text(point.payload.get("chunk_type")),
                    source_type=_text(point.payload.get("source_type")),
                    authority_level=_text(point.payload.get("authority_level")),
                )
            )
        # Ranks are renumbered after any drop so the fusion input is a contiguous 1..n ordering.
        ordered = tuple(
            LaneHit(
                chunk_id=hit.chunk_id,
                score=hit.score,
                rank=position,
                document_id=hit.document_id,
                document_version_id=hit.document_version_id,
                chunk_type=hit.chunk_type,
                source_type=hit.source_type,
                authority_level=hit.authority_level,
            )
            for position, hit in enumerate(hits, start=1)
        )
        return Ranking(
            lane="DENSE",
            hits=ordered,
            duration_ms=(time.perf_counter() - started) * 1000,
            scanned=len(points),
            weight=self.config.dense_weight,
        )


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None
