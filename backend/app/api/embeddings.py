"""Read-only M4 inspection and explicit audited re-embed requests.

Everything here enforces the same tenant boundary as the original document, and nothing returns a
dense vector, a collection credential or a lease token. Qdrant itself is never reachable from the
browser: the only index facts a client sees are the ones this server read on its behalf.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.api.documents import Actor, Service, correlation, job
from app.models.chunking import Chunk
from app.models.embeddings import (
    ChunkEmbedding,
    EmbeddingRun,
    EmbeddingVersion,
    IndexRun,
    IndexValidationFinding,
)
from app.repositories.embeddings import (
    get_embedding_run,
    get_embedding_version,
    get_index_run,
)
from app.repositories.parsing import scoped_version
from app.schemas.documents import JobView, Page
from app.schemas.embeddings import (
    ChunkEmbeddingView,
    EmbeddingRunView,
    EmbeddingSummaryView,
    EmbeddingVersionView,
    IndexFindingView,
    IndexRunView,
    IndexStatisticsView,
    ReembedRequest,
)

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


@router.post("/ingestion/jobs/{job_id}/reembed", response_model=JobView)
def reembed(
    job_id: UUID, body: ReembedRequest, request: Request, actor: Actor, service: Service
) -> JobView:
    if body.config is not None:
        actor.require("audit:read")
    service.embeddings.request(actor, job_id, correlation(request), body.force, body.config)
    return job(job_id, actor, service)


@router.get("/embedding-versions", response_model=Page[EmbeddingVersionView])
def versions(
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        query = select(EmbeddingVersion).order_by(
            EmbeddingVersion.created_at.desc(), EmbeddingVersion.id
        )
        return page(session, query, EmbeddingVersionView, offset, limit)


@router.get(
    "/documents/{document_id}/versions/{version_id}/embedding",
    response_model=EmbeddingSummaryView,
)
def summary(
    document_id: UUID, version_id: UUID, actor: Actor, service: Service
) -> EmbeddingSummaryView:
    """The current embedding and index state of one document version."""
    actor.require("document:read")
    with service.sessions() as session:
        version = scoped_version(session, actor.tenant_id, document_id, version_id)
        runs = session.scalars(
            select(EmbeddingRun)
            .where(
                EmbeddingRun.document_version_id == version_id,
                EmbeddingRun.tenant_id == actor.tenant_id,
            )
            .order_by(EmbeddingRun.created_at.desc())
        ).all()
        current = next((run for run in runs if run.is_active), runs[0] if runs else None)
        result = EmbeddingSummaryView(
            document_version_id=version_id,
            ingestion_status=version.ingestion_status.value,
            embedding_runs=len(runs),
        )
        if current is None:
            return result
        result.embedding_run = EmbeddingRunView.model_validate(current)
        result.embedding_version = EmbeddingVersionView.model_validate(
            get_embedding_version(session, current.embedding_version_id)
        )
        index_runs = session.scalars(
            select(IndexRun)
            .where(IndexRun.embedding_run_id == current.id)
            .order_by(IndexRun.attempt.desc())
        ).all()
        chosen = next(
            (run for run in index_runs if run.is_active), index_runs[0] if index_runs else None
        )
        if chosen is not None:
            result.index_run = IndexRunView.model_validate(chosen)
        result.finding_counts = {
            severity: count
            for severity, count in session.execute(
                select(IndexValidationFinding.severity, func.count())
                .where(IndexValidationFinding.embedding_run_id == current.id)
                .group_by(IndexValidationFinding.severity)
            ).all()
        }
        result.chunk_types = {
            chunk_type: count
            for chunk_type, count in session.execute(
                select(Chunk.chunk_type, func.count())
                .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
                .where(ChunkEmbedding.embedding_run_id == current.id)
                .group_by(Chunk.chunk_type)
            ).all()
        }
        return result


@router.get(
    "/documents/{document_id}/versions/{version_id}/embedding-runs",
    response_model=Page[EmbeddingRunView],
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
            select(EmbeddingRun)
            .where(
                EmbeddingRun.document_version_id == version_id,
                EmbeddingRun.tenant_id == actor.tenant_id,
            )
            .order_by(EmbeddingRun.created_at.desc(), EmbeddingRun.id)
        )
        return page(session, query, EmbeddingRunView, offset, limit)


@router.get("/embedding-runs/{run_id}", response_model=EmbeddingRunView)
def embedding_run(run_id: UUID, actor: Actor, service: Service) -> EmbeddingRunView:
    actor.require("document:read")
    with service.sessions() as session:
        return EmbeddingRunView.model_validate(get_embedding_run(session, actor.tenant_id, run_id))


@router.get("/embedding-runs/{run_id}/embeddings", response_model=Page[ChunkEmbeddingView])
def embeddings(
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    chunk_id: UUID | None = None,
    reused: bool | None = None,
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        get_embedding_run(session, actor.tenant_id, run_id)
        query = (
            select(ChunkEmbedding)
            .where(ChunkEmbedding.embedding_run_id == run_id)
            .order_by(ChunkEmbedding.chunk_id)
        )
        if chunk_id:
            query = query.where(ChunkEmbedding.chunk_id == chunk_id)
        if reused is not None:
            query = query.where(ChunkEmbedding.reused.is_(reused))
        return page(session, query, ChunkEmbeddingView, offset, limit)


@router.get("/embedding-runs/{run_id}/index-runs", response_model=Page[IndexRunView])
def index_runs(
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        get_embedding_run(session, actor.tenant_id, run_id)
        query = (
            select(IndexRun)
            .where(IndexRun.embedding_run_id == run_id)
            .order_by(IndexRun.attempt.desc())
        )
        return page(session, query, IndexRunView, offset, limit)


@router.get("/index-runs/{run_id}", response_model=IndexRunView)
def index_run(run_id: UUID, actor: Actor, service: Service) -> IndexRunView:
    actor.require("document:read")
    with service.sessions() as session:
        return IndexRunView.model_validate(get_index_run(session, actor.tenant_id, run_id))


@router.get("/index-runs/{run_id}/statistics", response_model=IndexStatisticsView)
def statistics(run_id: UUID, actor: Actor, service: Service) -> IndexStatisticsView:
    """Live point counts read from the vector index, scoped to this run and this tenant.

    Requires `document:manage`: it reaches a second system on the caller's behalf, so it is an
    operator tool rather than part of the ordinary reading path.
    """
    actor.require("document:manage")
    with service.sessions() as session:
        run = get_index_run(session, actor.tenant_id, run_id)
        embedding_version = get_embedding_version(session, run.embedding_version_id)
        expected = run.expected_point_count
        collection, vector_name, alias = run.physical_collection, run.vector_name, run.alias
        dimension = embedding_version.embedding_dimension
        distance = embedding_version.distance_metric
    index = service.vector_index
    if index is None:
        return IndexStatisticsView(
            index_run_id=run_id,
            collection=collection,
            vector_name=vector_name,
            dimension=dimension,
            distance_metric=distance,
            expected_points=expected,
            live_points=0,
            alias=alias,
            alias_target=None,
            reachable=False,
        )
    schema = service.embeddings.schema(collection)
    reachable = index.healthy()
    live = 0
    target = None
    if reachable:
        # Both scopes are enforced server-side; a client cannot widen them.
        live = index.count(
            schema, {"embedding_run_id": str(run.embedding_run_id), "tenant_id": str(run.tenant_id)}
        )
        target = index.alias_target(alias)
    return IndexStatisticsView(
        index_run_id=run_id,
        collection=collection,
        vector_name=vector_name,
        dimension=dimension,
        distance_metric=distance,
        expected_points=expected,
        live_points=live,
        alias=alias,
        alias_target=target,
        reachable=reachable,
    )


@router.get("/embedding-runs/{run_id}/validation-findings", response_model=Page[IndexFindingView])
def findings(
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    actor.require("document:read")
    with service.sessions() as session:
        get_embedding_run(session, actor.tenant_id, run_id)
        query = (
            select(IndexValidationFinding)
            .where(IndexValidationFinding.embedding_run_id == run_id)
            .order_by(IndexValidationFinding.created_at, IndexValidationFinding.id)
        )
        return page(session, query, IndexFindingView, offset, limit)
