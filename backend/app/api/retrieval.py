"""Authorized retrieval inspection and lexical-index inspection.

Every route enforces the same tenant boundary as the document it reads, and the tenant is always
taken from the authenticated principal — never from a path, a query parameter or a body.

`/retrieval/search` requires `retrieval:search`, which readers do not hold. It is a developer and
operator tool: it exposes lane scores, internal run identifiers and an execution trace. It is not
an answering endpoint, and its response is named `candidates` because that is what it returns.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.documents import Actor, Service, correlation, job
from app.api.embeddings import page
from app.core.errors import DomainError
from app.models.chunking import Chunk
from app.models.embeddings import IndexRun
from app.models.retrieval import (
    QueryEncoderVersion,
    SparseDocument,
    SparseIndex,
    SparseIndexVersion,
    SparseValidationFinding,
)
from app.repositories.parsing import scoped_version
from app.repositories.retrieval import resolve_corpus
from app.retrieval.errors import RetrievalError
from app.retrieval.model import RetrievalFilters
from app.schemas.documents import JobView, Page
from app.schemas.generation import DraftRequest, DraftResponse
from app.schemas.reranking import RerankRequest, RerankResponse
from app.schemas.retrieval import (
    CandidateView,
    LaneHitView,
    ProvenanceView,
    QueryEncoderVersionView,
    RetrievalStatusView,
    SearchRequest,
    SearchResponse,
    SparseFindingView,
    SparseIndexSummaryView,
    SparseIndexVersionView,
    SparseIndexView,
    SparseReindexRequest,
    TraceView,
)
from app.schemas.verification import AnswerRequest, AnswerResponse

router = APIRouter(prefix="/api/v1")


def _filters(
    body: SearchRequest | RerankRequest | DraftRequest | AnswerRequest,
) -> RetrievalFilters | None:
    if body.filters is None:
        return None
    return RetrievalFilters(
        document_ids=tuple(body.filters.document_ids),
        document_version_ids=tuple(body.filters.document_version_ids),
        chunk_types=tuple(body.filters.chunk_types),
        source_types=tuple(body.filters.source_types),
        authority_levels=tuple(body.filters.authority_levels),
        subjects=tuple(body.filters.subjects),
        specialties=tuple(body.filters.specialties),
    )


@router.post("/retrieval/search", response_model=SearchResponse)
def search(body: SearchRequest, request: Request, actor: Actor, service: Service) -> SearchResponse:
    """Return ranked evidence candidates. This endpoint never generates or summarises anything."""
    result = service.retrieval.search(
        actor,
        body.query,
        correlation(request),
        mode=body.mode,
        top_k=body.top_k,
        filters=_filters(body),
    )
    return SearchResponse(
        correlation_id=result.correlation_id,
        mode=result.mode,
        candidates=[
            CandidateView(
                chunk_id=hit.chunk_id,
                fused_rank=hit.fused_rank,
                fused_score=hit.fused_score,
                dense_rank=hit.dense_rank,
                dense_score=hit.dense_score,
                sparse_rank=hit.sparse_rank,
                sparse_score=hit.sparse_score,
                lanes=list(hit.lanes),
                matched_terms=list(hit.matched_terms),
                preview=hit.preview,
                provenance=ProvenanceView(
                    document_id=hit.provenance.document_id,
                    document_version_id=hit.provenance.document_version_id,
                    chunk_run_id=hit.provenance.chunk_run_id,
                    document_title=hit.provenance.document_title,
                    source_type=hit.provenance.source_type,
                    authority_level=hit.provenance.authority_level,
                    subject=hit.provenance.subject,
                    specialty=hit.provenance.specialty,
                    chunk_type=hit.provenance.chunk_type,
                    page_start=hit.provenance.page_start,
                    page_end=hit.provenance.page_end,
                    sequence_number=hit.provenance.sequence_number,
                    parent_chunk_id=hit.provenance.parent_chunk_id,
                    question_id=hit.provenance.question_id,
                    source_element_ids=list(hit.provenance.source_element_ids),
                ),
            )
            for hit in result.candidates
        ],
        dense=[_lane(hit) for hit in result.dense],
        sparse=[_lane(hit) for hit in result.sparse],
        trace=TraceView(
            correlation_id=result.trace.correlation_id,
            mode=result.trace.mode,
            retrieval_config_version=result.trace.retrieval_config_version,
            retrieval_config_fingerprint=result.trace.retrieval_config_fingerprint,
            query_encoder_version_id=result.trace.query_encoder_version_id,
            query_encoder_fingerprint=result.trace.query_encoder_fingerprint,
            query_hash=result.trace.query_hash,
            query_token_count=result.trace.query_token_count,
            normalization_version=result.trace.normalization_version,
            embedding_version_id=result.trace.embedding_version_id,
            sparse_index_version_id=result.trace.sparse_index_version_id,
            index_run_ids=list(result.trace.index_run_ids),
            sparse_index_ids=list(result.trace.sparse_index_ids),
            chunk_run_ids=list(result.trace.chunk_run_ids),
            dense_top_k=result.trace.dense_top_k,
            sparse_top_k=result.trace.sparse_top_k,
            final_top_k=result.trace.final_top_k,
            rrf_k=result.trace.rrf_k,
            dense_weight=result.trace.dense_weight,
            sparse_weight=result.trace.sparse_weight,
            bm25_k1=result.trace.bm25_k1,
            bm25_b=result.trace.bm25_b,
            dense_candidates=result.trace.dense_candidates,
            sparse_candidates=result.trace.sparse_candidates,
            fused_candidates=result.trace.fused_candidates,
            durations_ms=result.trace.durations_ms,
            query_vector_cached=result.trace.query_vector_cached,
        ),
        warnings=list(result.warnings),
    )


def _lane(hit: Any) -> LaneHitView:
    return LaneHitView(
        chunk_id=hit.chunk_id,
        rank=hit.rank,
        score=hit.score,
        chunk_type=hit.chunk_type,
        source_type=hit.source_type,
        authority_level=hit.authority_level,
        matched_terms=list(hit.matched_terms),
    )


@router.get("/retrieval/status", response_model=RetrievalStatusView)
def status(actor: Actor, service: Service) -> RetrievalStatusView:
    """What this tenant could search, and under which versions, without running a query."""
    actor.require("retrieval:search")
    settings = service.settings
    view = RetrievalStatusView(
        tenant_id=actor.tenant_id,
        document_versions=0,
        chunk_runs=0,
        dense_index_runs=0,
        sparse_indexes=0,
        analyzer=settings.sparse_analyzer,
        modes=["DENSE_ONLY", "BM25_ONLY", "HYBRID_RRF"],
        default_mode=settings.retrieval.mode,
        dense_top_k=settings.retrieval.dense_top_k,
        sparse_top_k=settings.retrieval.sparse_top_k,
        final_top_k=settings.retrieval.final_top_k,
        rrf_k=settings.retrieval.rrf_k,
        bm25_k1=settings.retrieval.bm25_k1,
        bm25_b=settings.retrieval.bm25_b,
        degradation_policy=settings.retrieval.degradation_policy,
    )
    with service.sessions() as session:
        try:
            corpus = resolve_corpus(session, actor.tenant_id)
        except RetrievalError as exc:
            # A misaligned corpus is reported here rather than raised, so an operator can see the
            # reason on the status page instead of only discovering it by running a query.
            view.corpus_error = exc.code
            return view
        view.document_versions = len(corpus.document_version_ids)
        view.chunk_runs = len(corpus.chunk_run_ids)
        view.dense_index_runs = len(corpus.index_run_ids)
        view.sparse_indexes = len(corpus.sparse_index_ids)
        view.embedding_version_id = corpus.embedding_version_id
        view.sparse_index_version_id = corpus.sparse_index_version_id
        encoder = session.scalars(
            select(QueryEncoderVersion).order_by(QueryEncoderVersion.created_at.desc())
        ).first()
        if encoder is not None:
            view.query_encoder = QueryEncoderVersionView.model_validate(encoder)
    return view


@router.get("/query-encoder-versions", response_model=Page[QueryEncoderVersionView])
def encoder_versions(
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        query = select(QueryEncoderVersion).order_by(
            QueryEncoderVersion.created_at.desc(), QueryEncoderVersion.id
        )
        return page(session, query, QueryEncoderVersionView, offset, limit)


@router.get("/sparse-index-versions", response_model=Page[SparseIndexVersionView])
def analyzer_versions(
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        query = select(SparseIndexVersion).order_by(
            SparseIndexVersion.created_at.desc(), SparseIndexVersion.id
        )
        return page(session, query, SparseIndexVersionView, offset, limit)


@router.get(
    "/documents/{document_id}/versions/{version_id}/sparse-index",
    response_model=SparseIndexSummaryView,
)
def summary(
    document_id: UUID, version_id: UUID, actor: Actor, service: Service
) -> SparseIndexSummaryView:
    actor.require("document:read")
    with service.sessions() as session:
        version = scoped_version(session, actor.tenant_id, document_id, version_id)
        indexes = session.scalars(
            select(SparseIndex)
            .where(
                SparseIndex.document_version_id == version_id,
                SparseIndex.tenant_id == actor.tenant_id,
            )
            .order_by(SparseIndex.created_at.desc())
        ).all()
        current = next((row for row in indexes if row.is_active), indexes[0] if indexes else None)
        result = SparseIndexSummaryView(
            document_version_id=version_id,
            ingestion_status=version.ingestion_status.value,
            sparse_indexes=len(indexes),
        )
        if current is None:
            return result
        result.sparse_index = SparseIndexView.model_validate(current)
        analyzer = session.get(SparseIndexVersion, current.sparse_index_version_id)
        if analyzer is not None:
            result.sparse_index_version = SparseIndexVersionView.model_validate(analyzer)
        dense = session.scalar(
            select(IndexRun).where(
                IndexRun.document_version_id == version_id,
                IndexRun.tenant_id == actor.tenant_id,
                IndexRun.is_active.is_(True),
                IndexRun.status == "VERIFIED",
            )
        )
        result.lanes_aligned = bool(
            dense is not None
            and current.is_active
            and current.status == "VERIFIED"
            and dense.chunk_run_id == current.chunk_run_id
        )
        # Searchable, not answerable. Nothing in this response asserts the latter.
        result.retrieval_ready = result.lanes_aligned and version.ingestion_status.value == (
            "RETRIEVAL_READY"
        )
        result.finding_counts = {
            severity: count
            for severity, count in session.execute(
                select(SparseValidationFinding.severity, func.count())
                .where(SparseValidationFinding.sparse_index_id == current.id)
                .group_by(SparseValidationFinding.severity)
            ).all()
        }
        result.chunk_types = {
            chunk_type: count
            for chunk_type, count in session.execute(
                select(Chunk.chunk_type, func.count())
                .join(SparseDocument, SparseDocument.chunk_id == Chunk.id)
                .where(SparseDocument.sparse_index_id == current.id)
                .group_by(Chunk.chunk_type)
            ).all()
        }
        return result


def _scoped_index(session: Session, tenant_id: UUID, index_id: UUID) -> SparseIndex:
    found: SparseIndex | None = session.scalar(
        select(SparseIndex).where(SparseIndex.id == index_id, SparseIndex.tenant_id == tenant_id)
    )
    if found is None:
        raise DomainError("SPARSE_INDEX_NOT_FOUND", "Lexical index not found.", 404)
    return found


@router.get("/sparse-indexes/{index_id}", response_model=SparseIndexView)
def sparse_index(index_id: UUID, actor: Actor, service: Service) -> SparseIndexView:
    actor.require("document:read")
    with service.sessions() as session:
        return SparseIndexView.model_validate(_scoped_index(session, actor.tenant_id, index_id))


@router.get(
    "/sparse-indexes/{index_id}/validation-findings", response_model=Page[SparseFindingView]
)
def sparse_findings(
    index_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        _scoped_index(session, actor.tenant_id, index_id)
        query = (
            select(SparseValidationFinding)
            .where(SparseValidationFinding.sparse_index_id == index_id)
            .order_by(SparseValidationFinding.created_at, SparseValidationFinding.id)
        )
        return page(session, query, SparseFindingView, offset, limit)


@router.post("/ingestion/jobs/{job_id}/reindex-sparse", response_model=JobView)
def reindex_sparse(
    job_id: UUID,
    body: SparseReindexRequest,
    request: Request,
    actor: Actor,
    service: Service,
) -> JobView:
    """Rebuild the lexical lane alone, for instance after an analyzer change."""
    if body.analyzer is not None:
        actor.require("audit:read")
    service.sparse.request(actor, job_id, correlation(request), body.analyzer)
    return job(job_id, actor, service)


@router.post("/retrieval/rerank", response_model=RerankResponse)
def rerank(body: RerankRequest, request: Request, actor: Actor, service: Service) -> dict[str, Any]:
    actor.require("retrieval:search")
    if body.mode not in (None, "HYBRID_RRF"):
        raise DomainError("INVALID_REQUEST", "M6 requires hybrid retrieval.", 422)
    return service.evidence.search(actor, body.query, correlation(request), _filters(body))


@router.post("/retrieval/draft", response_model=DraftResponse)
async def draft(
    body: DraftRequest, request: Request, actor: Actor, service: Service
) -> dict[str, Any]:
    """Retrieve, rerank, gate, and generate a grounded draft only if the gate permits it.

    The response is an authorized inspection artifact: it carries the sufficiency decision whether
    or not a draft was produced, and it is never a verified answer.
    """
    actor.require("retrieval:search")
    actor.require("generation:draft")
    return await service.generation.draft(actor, body.query, correlation(request), _filters(body))


@router.post("/retrieval/answer", response_model=AnswerResponse)
async def answer(
    body: AnswerRequest, request: Request, actor: Actor, service: Service
) -> dict[str, Any]:
    """Retrieve, gate, draft, then verify every material claim before anything is released.

    Returns a verified answer only when every material claim survived verification and the evidence
    does not disagree with itself. Every other outcome is a typed abstention, and the full
    verification report is returned either way so a refusal can be read rather than guessed at.
    """
    actor.require("retrieval:search")
    actor.require("generation:draft")
    actor.require("generation:verify")
    return await service.verification.answer(
        actor, body.query, correlation(request), _filters(body)
    )
