"""Read-only chunk/provenance inspection and explicit audited rechunk requests."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.api.documents import Actor, Service, correlation, job
from app.core.errors import DomainError
from app.models.chunking import (
    Chunk,
    ChunkArtifactRelation,
    ChunkRelation,
    ChunkRun,
    ChunkSourceElement,
    ChunkValidationFinding,
    QuestionArtifact,
    QuestionOption,
)
from app.models.parsing import DocumentElement
from app.repositories.chunking import get_chunk, get_run
from app.repositories.parsing import scoped_version
from app.schemas.chunking import (
    ArtifactLinkView,
    ChunkDetailView,
    ChunkRunView,
    ChunkView,
    FindingView,
    OptionView,
    QuestionView,
    RechunkRequest,
    SourceView,
)
from app.schemas.documents import JobView, Page
from app.schemas.parsing import ElementView, box

router = APIRouter(prefix="/api/v1")


def page(session: Any, query: Any, schema: Any, offset: int, limit: int) -> Any:
    total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    return Page(
        items=[
            schema.model_validate(row) for row in session.scalars(query.offset(offset).limit(limit))
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post("/ingestion/jobs/{job_id}/rechunk", response_model=JobView)
def rechunk(
    job_id: UUID, body: RechunkRequest, request: Request, actor: Actor, service: Service
) -> JobView:
    if body.config is not None:
        actor.require("audit:read")
    service.chunks.request(actor, job_id, correlation(request), body.force, body.config)
    return job(job_id, actor, service)


@router.get(
    "/documents/{document_id}/versions/{version_id}/chunk-runs", response_model=Page[ChunkRunView]
)
def runs(
    document_id: UUID,
    version_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        scoped_version(session, actor.tenant_id, document_id, version_id)
        query = (
            select(ChunkRun)
            .where(
                ChunkRun.document_version_id == version_id, ChunkRun.tenant_id == actor.tenant_id
            )
            .order_by(ChunkRun.created_at.desc(), ChunkRun.id)
        )
        return page(session, query, ChunkRunView, offset, limit)


@router.get("/chunk-runs/{run_id}", response_model=ChunkRunView)
def run(run_id: UUID, actor: Actor, service: Service) -> ChunkRunView:
    actor.require("document:read")
    with service.sessions() as session:
        return ChunkRunView.model_validate(get_run(session, actor.tenant_id, run_id))


@router.get("/chunk-runs/{run_id}/chunks", response_model=Page[ChunkView])
def chunks(
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    chunk_type: str | None = Query(None, max_length=40),
    page_number: int | None = Query(None, ge=1),
    parent_id: UUID | None = None,
    question_id: UUID | None = None,
    table_id: UUID | None = None,
    figure_id: UUID | None = None,
    formula_id: UUID | None = None,
    warnings: bool = False,
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        get_run(session, actor.tenant_id, run_id)
        query = select(Chunk).where(Chunk.chunk_run_id == run_id).order_by(Chunk.sequence_number)
        if chunk_type:
            query = query.where(Chunk.chunk_type == chunk_type)
        if page_number:
            query = query.where(Chunk.page_start <= page_number, Chunk.page_end >= page_number)
        if parent_id:
            query = query.where(Chunk.parent_chunk_id == parent_id)
        if question_id:
            query = query.where(Chunk.question_id == question_id)
        for column, value in (
            (ChunkArtifactRelation.table_id, table_id),
            (ChunkArtifactRelation.figure_id, figure_id),
            (ChunkArtifactRelation.formula_id, formula_id),
        ):
            if value:
                query = query.where(
                    Chunk.id.in_(select(ChunkArtifactRelation.chunk_id).where(column == value))
                )
        if warnings:
            query = query.where(
                Chunk.id.in_(
                    select(ChunkValidationFinding.chunk_id).where(
                        ChunkValidationFinding.chunk_run_id == run_id
                    )
                )
            )
        return page(session, query, ChunkView, offset, limit)


@router.get("/chunks/{chunk_id}", response_model=ChunkDetailView)
def chunk(chunk_id: UUID, actor: Actor, service: Service) -> ChunkDetailView:
    actor.require("document:read")
    with service.sessions() as session:
        result = ChunkDetailView.model_validate(get_chunk(session, actor.tenant_id, chunk_id))
        result.artifacts = [
            ArtifactLinkView.model_validate(row)
            for row in session.scalars(
                select(ChunkArtifactRelation).where(ChunkArtifactRelation.chunk_id == chunk_id)
            )
        ]
        result.next_sibling_ids = list(
            session.scalars(
                select(ChunkRelation.target_id).where(
                    ChunkRelation.source_id == chunk_id, ChunkRelation.relation == "NEXT_SIBLING"
                )
            )
        )
        return result


@router.get("/chunks/{chunk_id}/sources", response_model=Page[SourceView])
def sources(
    chunk_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        get_chunk(session, actor.tenant_id, chunk_id)
        query = (
            select(ChunkSourceElement)
            .where(ChunkSourceElement.chunk_id == chunk_id)
            .order_by(ChunkSourceElement.position)
        )
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        items = []
        for link in session.scalars(query.offset(offset).limit(limit)):
            element = session.get(DocumentElement, link.element_id)
            if element is None:
                raise DomainError("CHUNK_SOURCE_NOT_FOUND", "Source element not found.", 404)
            view = ElementView.model_validate(element)
            view.bbox = box(element)
            items.append(
                SourceView(
                    element=view,
                    position=link.position,
                    start_offset=link.start_offset,
                    end_offset=link.end_offset,
                    role=link.role,
                )
            )
        return Page(items=items, total=total, offset=offset, limit=limit)


@router.get("/chunk-runs/{run_id}/questions", response_model=Page[QuestionView])
def questions(
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        get_run(session, actor.tenant_id, run_id)
        query = (
            select(QuestionArtifact)
            .where(QuestionArtifact.chunk_run_id == run_id)
            .order_by(QuestionArtifact.page_start, QuestionArtifact.id)
        )
        result = page(session, query, QuestionView, offset, limit)
        for item in result.items:
            item.options = [
                OptionView.model_validate(row)
                for row in session.scalars(
                    select(QuestionOption)
                    .where(QuestionOption.question_id == item.id)
                    .order_by(QuestionOption.ordinal)
                )
            ]
        return result


@router.get("/chunk-runs/{run_id}/validation-findings", response_model=Page[FindingView])
def findings(
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        get_run(session, actor.tenant_id, run_id)
        return page(
            session,
            select(ChunkValidationFinding)
            .where(ChunkValidationFinding.chunk_run_id == run_id)
            .order_by(ChunkValidationFinding.created_at, ChunkValidationFinding.id),
            FindingView,
            offset,
            limit,
        )
