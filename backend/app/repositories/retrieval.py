"""Corpus resolution, lexical search and provenance hydration, all tenant-scoped in PostgreSQL.

The single most important function here is `resolve_corpus`. It answers "what is this tenant
allowed to search, right now?" and every lane is confined to its answer. It reads durable
relational state — not the Qdrant alias, not a cache, not a request parameter — and it fails
closed rather than returning a partially consistent corpus.
"""

import time
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.retrieval_config import RetrievalConfig, SparseAnalyzerConfig
from app.models.chunking import Chunk, ChunkRun, ChunkSourceElement
from app.models.documents import Document, DocumentVersion
from app.models.embeddings import IndexRun
from app.models.enums import Status
from app.models.retrieval import SparseDocument, SparseIndex, SparsePosting, SparseTerm
from app.retrieval.errors import RetrievalError
from app.retrieval.model import (
    LaneHit,
    Provenance,
    Ranking,
    RetrievalCorpus,
    RetrievalFilters,
)
from app.retrieval.sparse.analyzer import query_terms
from app.retrieval.sparse.bm25 import CorpusStatistics, Posting, rank


def resolve_corpus(session: Session, tenant_id: UUID) -> RetrievalCorpus:
    """The document versions this tenant may search, and the run identifiers that bound each lane.

    A version qualifies only when **all** of the following hold, which is what makes a fused
    ranking meaningful:

    * its ingestion job reached RETRIEVAL_READY;
    * it has an active, VERIFIED dense index run;
    * it has an active, VERIFIED lexical index;
    * both were built from the same chunk dataset;
    * the document is not archived.

    A version still being embedded or reindexed simply is not in the corpus yet — that is
    incompleteness, not inconsistency, and it is the existing posture for every earlier stage. A
    version whose two lanes disagree about the chunk dataset is a different matter and fails the
    whole query: it should be impossible by construction, and silently searching it would fuse
    rankings computed over different text.
    """
    rows = session.execute(
        select(
            DocumentVersion.id,
            IndexRun.id,
            IndexRun.embedding_run_id,
            IndexRun.chunk_run_id,
            IndexRun.embedding_version_id,
            IndexRun.physical_collection,
            IndexRun.vector_name,
            SparseIndex.id,
            SparseIndex.chunk_run_id,
            SparseIndex.sparse_index_version_id,
        )
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(
            IndexRun,
            (IndexRun.document_version_id == DocumentVersion.id)
            & IndexRun.is_active
            & (IndexRun.status == "VERIFIED"),
        )
        .join(
            SparseIndex,
            (SparseIndex.document_version_id == DocumentVersion.id)
            & SparseIndex.is_active
            & (SparseIndex.status == "VERIFIED"),
        )
        .where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.ingestion_status == Status.RETRIEVAL_READY,
            Document.archived_at.is_(None),
        )
        .order_by(DocumentVersion.id)
    ).all()

    misaligned = [str(row[0]) for row in rows if row[3] != row[8]]
    if misaligned:
        raise RetrievalError(
            "RETRIEVAL_CORPUS_MISALIGNED",
            {"document_version_ids": misaligned[:20], "count": len(misaligned)},
        )

    collections = {row[5] for row in rows}
    vector_names = {row[6] for row in rows}
    embedding_versions = {row[4] for row in rows}
    analyzers = {row[9] for row in rows}
    if len(collections) > 1 or len(vector_names) > 1 or len(embedding_versions) > 1:
        # Two vector spaces cannot be searched with one query vector, and comparing scores across
        # them would be meaningless. Re-embed the corpus onto one version rather than mixing.
        raise RetrievalError(
            "RETRIEVAL_CORPUS_MISALIGNED",
            {"reason": "multiple embedding versions are active", "collections": len(collections)},
        )
    if len(analyzers) > 1:
        raise RetrievalError("SPARSE_INDEX_VERSION_MISMATCH", {"analyzer_versions": len(analyzers)})

    return RetrievalCorpus(
        tenant_id=tenant_id,
        document_version_ids=tuple(row[0] for row in rows),
        embedding_run_ids=tuple(row[2] for row in rows),
        index_run_ids=tuple(row[1] for row in rows),
        sparse_index_ids=tuple(row[7] for row in rows),
        chunk_run_ids=tuple(dict.fromkeys(row[3] for row in rows)),
        embedding_version_id=next(iter(embedding_versions), None),
        sparse_index_version_id=next(iter(analyzers), None),
        collection=next(iter(collections), None),
        vector_name=next(iter(vector_names), None),
    )


class PostgresSparseRetriever:
    """`SparseRetriever` over the durable postings.

    Statistics are computed over exactly the supplied indexes, so inverse document frequency
    describes this tenant's active corpus and nothing else.
    """

    def __init__(
        self,
        session: Session,
        analyzer: SparseAnalyzerConfig,
        config: RetrievalConfig,
    ) -> None:
        self.session = session
        self.analyzer = analyzer
        self.config = config

    def statistics(self, index_ids: tuple[UUID, ...], terms: tuple[str, ...]) -> CorpusStatistics:
        documents, length = self.session.execute(
            select(
                func.coalesce(func.sum(SparseIndex.indexed_chunk_count), 0),
                func.coalesce(func.sum(SparseIndex.total_length), 0),
            ).where(SparseIndex.id.in_(index_ids))
        ).one()
        frequencies: dict[str, int] = {
            term: int(count)
            for term, count in self.session.execute(
                select(SparseTerm.term, func.coalesce(func.sum(SparseTerm.document_frequency), 0))
                .where(
                    SparseTerm.sparse_index_id.in_(index_ids),
                    SparseTerm.term.in_(terms),
                )
                .group_by(SparseTerm.term)
            ).all()
        }
        return CorpusStatistics(
            document_count=int(documents),
            total_length=int(length),
            document_frequency=frequencies,
        )

    def search(
        self,
        query: str,
        *,
        corpus: RetrievalCorpus,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> Ranking:
        started = time.perf_counter()
        if corpus.empty or not corpus.sparse_index_ids:
            raise RetrievalError("SPARSE_INDEX_NOT_READY")
        terms = query_terms(query, self.analyzer)
        if not terms:
            return Ranking(
                lane="BM25",
                hits=(),
                duration_ms=(time.perf_counter() - started) * 1000,
                weight=self.config.sparse_weight,
            )
        statistics = self.statistics(corpus.sparse_index_ids, terms)
        scanned = sum(statistics.document_frequency.values())
        if scanned > self.config.max_scanned_postings:
            # Refused rather than silently truncated: dropping postings would quietly change
            # which documents could be found, and the caller would have no way to know.
            raise RetrievalError(
                "SPARSE_QUERY_TOO_BROAD",
                {"postings": scanned, "limit": self.config.max_scanned_postings},
            )

        statement = (
            select(
                SparsePosting.chunk_id,
                SparsePosting.term,
                SparsePosting.term_frequency,
                SparseDocument.length,
                SparseDocument.document_id,
                SparseDocument.document_version_id,
                SparseDocument.chunk_type,
                SparseDocument.source_type,
                SparseDocument.authority_level,
                SparseDocument.subject,
                SparseDocument.specialty,
            )
            .join(
                SparseDocument,
                (SparseDocument.sparse_index_id == SparsePosting.sparse_index_id)
                & (SparseDocument.chunk_id == SparsePosting.chunk_id),
            )
            .where(
                SparsePosting.sparse_index_id.in_(corpus.sparse_index_ids),
                SparsePosting.term.in_(terms),
                # Redundant with the index scope by construction, and asserted anyway: a tenant
                # condition that is only implied is a tenant condition waiting to be removed.
                SparseDocument.tenant_id == corpus.tenant_id,
            )
        )
        try:
            rows = self.session.execute(statement).all()
        except Exception as exc:  # noqa: BLE001 - database errors map to a declared code
            raise RetrievalError("SPARSE_SEARCH_FAILED") from exc

        postings = [
            Posting(
                chunk_id=row.chunk_id,
                term=row.term,
                term_frequency=row.term_frequency,
                length=row.length,
                document_id=row.document_id,
                document_version_id=row.document_version_id,
                chunk_type=row.chunk_type,
                source_type=row.source_type,
                authority_level=row.authority_level,
                subject=row.subject,
                specialty=row.specialty,
            )
            for row in rows
        ]
        scored = rank(
            postings,
            statistics,
            terms,
            k1=self.config.bm25_k1,
            b=self.config.bm25_b,
            top_k=top_k,
            filters=filters,
        )
        facets = {posting.chunk_id: posting for posting in postings}
        hits = tuple(
            LaneHit(
                chunk_id=chunk_id,
                score=score,
                rank=position,
                document_id=facets[chunk_id].document_id,
                document_version_id=facets[chunk_id].document_version_id,
                chunk_type=facets[chunk_id].chunk_type,
                source_type=facets[chunk_id].source_type,
                authority_level=facets[chunk_id].authority_level,
                matched_terms=matched,
            )
            for position, (chunk_id, score, matched) in enumerate(scored, start=1)
        )
        return Ranking(
            lane="BM25",
            hits=hits,
            duration_ms=(time.perf_counter() - started) * 1000,
            scanned=len(postings),
            weight=self.config.sparse_weight,
        )


def hydrate(
    session: Session,
    tenant_id: UUID,
    chunk_ids: tuple[UUID, ...],
    corpus: RetrievalCorpus,
    preview_characters: int,
) -> dict[UUID, tuple[Provenance, str]]:
    """Resolve candidates to canonical chunk content and full provenance.

    Chunk identifiers arriving here came from this server's own lanes, but they are re-scoped
    against the tenant and the resolved corpus anyway. A retrieval path that trusted an identifier
    because "it came from our index" would be one refactor away from trusting one that came from a
    request body.

    The preview is drawn from the canonical chunk in PostgreSQL, never from an index payload: the
    index deliberately stores no chunk text, and provenance that came back from the same system
    being audited would not be independent evidence of anything.
    """
    if not chunk_ids:
        return {}
    rows = session.execute(
        select(Chunk, ChunkRun, Document)
        .join(ChunkRun, ChunkRun.id == Chunk.chunk_run_id)
        .join(Document, Document.id == ChunkRun.document_id)
        .where(
            Chunk.id.in_(chunk_ids),
            Chunk.tenant_id == tenant_id,
            ChunkRun.document_version_id.in_(corpus.document_version_ids),
            ChunkRun.id.in_(corpus.chunk_run_ids),
            ChunkRun.is_active.is_(True),
            Document.archived_at.is_(None),
        )
    ).all()

    elements: dict[UUID, list[UUID]] = {}
    for chunk_id, element_id in session.execute(
        select(ChunkSourceElement.chunk_id, ChunkSourceElement.element_id)
        .where(
            ChunkSourceElement.chunk_id.in_(chunk_ids),
            # HIERARCHY links are ancestry, not the source text of this chunk; keeping them out
            # means a candidate's source elements are exactly the elements it was built from.
            ChunkSourceElement.role != "HIERARCHY",
        )
        .order_by(ChunkSourceElement.chunk_id, ChunkSourceElement.position)
    ).all():
        elements.setdefault(chunk_id, []).append(element_id)

    resolved: dict[UUID, tuple[Provenance, str]] = {}
    for chunk, run, document in rows:
        preview = chunk.retrieval_text[:preview_characters] if preview_characters else ""
        resolved[chunk.id] = (
            Provenance(
                document_id=document.id,
                document_version_id=run.document_version_id,
                chunk_run_id=run.id,
                document_title=document.title,
                source_type=document.source_type.value,
                authority_level=document.authority_level.value,
                subject=document.subject,
                specialty=document.specialty,
                chunk_type=chunk.chunk_type,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                sequence_number=chunk.sequence_number,
                parent_chunk_id=chunk.parent_chunk_id,
                question_id=chunk.question_id,
                source_element_ids=tuple(elements.get(chunk.id, ())),
            ),
            preview,
        )
    return resolved
