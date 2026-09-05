"""Tenant-authorized parse inspection API.

Every route resolves the owning document and version first, under the caller's tenant, and only
then the parse-run child resource. Binary artifacts (raw parser output, page previews, figure
crops) stream through these endpoints; object storage is never exposed and no pre-signed URL is
issued. These are inspection endpoints: nothing here starts, changes or approves a parse.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select

from app.api.documents import Actor, Service
from app.core.errors import DomainError
from app.models.enums import ElementType, Severity
from app.models.parsing import (
    DocumentElement,
    DocumentPage,
    FigureArtifact,
    FormulaArtifact,
    ParseRun,
    ParseValidationFinding,
    TableArtifact,
)
from app.repositories.parsing import (
    get_element,
    get_figure,
    get_formula,
    get_page,
    get_parse_run,
    get_table,
    latest_parse_run,
    scoped_version,
)
from app.schemas.documents import Page
from app.schemas.parsing import (
    ElementView,
    FigureView,
    FindingView,
    FormulaView,
    PageDetailView,
    PageView,
    ParseRunView,
    ParseSummaryView,
    TableDetailView,
    TableView,
    box,
)

router = APIRouter(prefix="/api/v1")


def _run_view(session: Any, run: ParseRun) -> ParseRunView:
    view = ParseRunView.model_validate(run)
    counts = session.execute(
        select(ParseValidationFinding.severity, func.count())
        .where(ParseValidationFinding.parse_run_id == run.id)
        .group_by(ParseValidationFinding.severity)
    ).all()
    view.finding_counts = {severity.value: int(count) for severity, count in counts}
    return view


def _resolve(
    session: Any, tenant_id: UUID, document_id: UUID, version_id: UUID, run_id: UUID
) -> ParseRun:
    scoped_version(session, tenant_id, document_id, version_id)
    run = get_parse_run(session, tenant_id, run_id)
    if run.document_version_id != version_id:
        raise DomainError("PARSE_RUN_NOT_FOUND", "Parse run not found.", 404)
    return run


Base = "/documents/{document_id}/versions/{version_id}/parse-runs"


@router.get("/documents/{document_id}/versions/{version_id}/parse", response_model=ParseSummaryView)
def parse_summary(
    document_id: UUID, version_id: UUID, actor: Actor, service: Service
) -> ParseSummaryView:
    """Current parse state for a version: the active run, else the most recent attempt."""
    actor.require("document:read")
    with service.sessions() as session:
        version = scoped_version(session, actor.tenant_id, document_id, version_id)
        run = latest_parse_run(session, actor.tenant_id, version_id)
        total = int(
            session.scalar(
                select(func.count())
                .select_from(ParseRun)
                .where(
                    ParseRun.document_version_id == version_id,
                    ParseRun.tenant_id == actor.tenant_id,
                )
            )
            or 0
        )
        return ParseSummaryView(
            document_version_id=version.id,
            ingestion_status=version.ingestion_status.value,
            parse_run=_run_view(session, run) if run else None,
            parse_runs=total,
        )


@router.get(Base, response_model=Page[ParseRunView])
def parse_runs(
    document_id: UUID,
    version_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Page[ParseRunView]:
    actor.require("document:read")
    with service.sessions() as session:
        scoped_version(session, actor.tenant_id, document_id, version_id)
        query = select(ParseRun).where(
            ParseRun.document_version_id == version_id, ParseRun.tenant_id == actor.tenant_id
        )
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(query.order_by(ParseRun.attempt.desc()).offset(offset).limit(limit))
        return Page(
            items=[_run_view(session, row) for row in rows],
            total=total,
            offset=offset,
            limit=limit,
        )


@router.get(Base + "/{run_id}", response_model=ParseRunView)
def parse_run(
    document_id: UUID, version_id: UUID, run_id: UUID, actor: Actor, service: Service
) -> ParseRunView:
    actor.require("document:read")
    with service.sessions() as session:
        return _run_view(
            session, _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        )


@router.get(Base + "/{run_id}/pages", response_model=Page[PageView])
def pages(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> Page[PageView]:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        query = select(DocumentPage).where(DocumentPage.parse_run_id == run.id)
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(query.order_by(DocumentPage.page_number).offset(offset).limit(limit))
        items = []
        for row in rows:
            view = PageView.model_validate(row)
            view.has_preview = row.preview_key is not None
            items.append(view)
        return Page(items=items, total=total, offset=offset, limit=limit)


@router.get(Base + "/{run_id}/pages/{page_id}", response_model=PageDetailView)
def page_detail(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    page_id: UUID,
    actor: Actor,
    service: Service,
) -> PageDetailView:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        row = get_page(session, actor.tenant_id, run, page_id)
        view = PageDetailView.model_validate(row)
        view.has_preview = row.preview_key is not None
        return view


@router.get(Base + "/{run_id}/pages/{page_id}/preview")
def page_preview(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    page_id: UUID,
    actor: Actor,
    service: Service,
) -> Response:
    """Stream the stored page preview. Previews are optional; absence is reported, not faked."""
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        row = get_page(session, actor.tenant_id, run, page_id)
        if not row.preview_key or not row.preview_version_id:
            raise DomainError("PAGE_PREVIEW_UNAVAILABLE", "No preview exists for this page.", 404)
        key, object_version = row.preview_key, row.preview_version_id
        media_type = row.preview_media_type or "application/octet-stream"
    return _stream(service, key, object_version, media_type)


@router.get(Base + "/{run_id}/elements", response_model=Page[ElementView])
def elements(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    page_number: int | None = Query(None, ge=1),
    element_type: ElementType | None = None,
) -> Page[ElementView]:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        query = select(DocumentElement).where(DocumentElement.parse_run_id == run.id)
        if page_number is not None:
            query = query.where(DocumentElement.page_number == page_number)
        if element_type is not None:
            query = query.where(DocumentElement.element_type == element_type)
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(DocumentElement.reading_order).offset(offset).limit(limit)
        )
        items = []
        for row in rows:
            view = ElementView.model_validate(row)
            view.bbox = box(row)
            items.append(view)
        return Page(items=items, total=total, offset=offset, limit=limit)


@router.get(Base + "/{run_id}/elements/{element_id}", response_model=ElementView)
def element_detail(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    element_id: UUID,
    actor: Actor,
    service: Service,
) -> ElementView:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        row = get_element(session, actor.tenant_id, run, element_id)
        view = ElementView.model_validate(row)
        view.bbox = box(row)
        return view


@router.get(Base + "/{run_id}/tables", response_model=Page[TableView])
def tables(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    page_number: int | None = Query(None, ge=1),
) -> Page[TableView]:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        query = select(TableArtifact).where(TableArtifact.parse_run_id == run.id)
        if page_number is not None:
            query = query.where(TableArtifact.page_number == page_number)
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(TableArtifact.page_number, TableArtifact.created_at)
            .offset(offset)
            .limit(limit)
        )
        items = []
        for row in rows:
            view = TableView.model_validate(row)
            view.bbox = box(row)
            items.append(view)
        return Page(items=items, total=total, offset=offset, limit=limit)


@router.get(Base + "/{run_id}/tables/{table_id}", response_model=TableDetailView)
def table_detail(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    table_id: UUID,
    actor: Actor,
    service: Service,
) -> TableDetailView:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        row = get_table(session, actor.tenant_id, run, table_id)
        view = TableDetailView.model_validate(row)
        view.bbox = box(row)
        return view


@router.get(Base + "/{run_id}/figures", response_model=Page[FigureView])
def figures(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    page_number: int | None = Query(None, ge=1),
) -> Page[FigureView]:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        query = select(FigureArtifact).where(FigureArtifact.parse_run_id == run.id)
        if page_number is not None:
            query = query.where(FigureArtifact.page_number == page_number)
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(FigureArtifact.page_number, FigureArtifact.created_at)
            .offset(offset)
            .limit(limit)
        )
        items = []
        for row in rows:
            view = FigureView.model_validate(row)
            view.bbox = box(row)
            view.has_image = row.image_key is not None
            items.append(view)
        return Page(items=items, total=total, offset=offset, limit=limit)


@router.get(Base + "/{run_id}/figures/{figure_id}/image")
def figure_image(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    figure_id: UUID,
    actor: Actor,
    service: Service,
) -> Response:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        row = get_figure(session, actor.tenant_id, run, figure_id)
        if not row.image_key or not row.image_version_id:
            raise DomainError(
                "FIGURE_IMAGE_UNAVAILABLE", "No image artifact exists for this figure.", 404
            )
        key, object_version = row.image_key, row.image_version_id
        media_type = row.image_media_type or "application/octet-stream"
    return _stream(service, key, object_version, media_type)


@router.get(Base + "/{run_id}/formulas", response_model=Page[FormulaView])
def formulas(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    page_number: int | None = Query(None, ge=1),
) -> Page[FormulaView]:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        query = select(FormulaArtifact).where(FormulaArtifact.parse_run_id == run.id)
        if page_number is not None:
            query = query.where(FormulaArtifact.page_number == page_number)
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(FormulaArtifact.page_number, FormulaArtifact.created_at)
            .offset(offset)
            .limit(limit)
        )
        items = []
        for row in rows:
            view = FormulaView.model_validate(row)
            view.bbox = box(row)
            items.append(view)
        return Page(items=items, total=total, offset=offset, limit=limit)


@router.get(Base + "/{run_id}/formulas/{formula_id}", response_model=FormulaView)
def formula_detail(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    formula_id: UUID,
    actor: Actor,
    service: Service,
) -> FormulaView:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        row = get_formula(session, actor.tenant_id, run, formula_id)
        view = FormulaView.model_validate(row)
        view.bbox = box(row)
        return view


@router.get(Base + "/{run_id}/findings", response_model=Page[FindingView])
def findings(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    severity: Severity | None = None,
    page_number: int | None = Query(None, ge=1),
) -> Page[FindingView]:
    actor.require("document:read")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        query = select(ParseValidationFinding).where(ParseValidationFinding.parse_run_id == run.id)
        if severity is not None:
            query = query.where(ParseValidationFinding.severity == severity)
        if page_number is not None:
            query = query.where(ParseValidationFinding.page_number == page_number)
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(
                ParseValidationFinding.severity.desc(), ParseValidationFinding.created_at
            )
            .offset(offset)
            .limit(limit)
        )
        return Page(
            items=[FindingView.model_validate(row) for row in rows],
            total=total,
            offset=offset,
            limit=limit,
        )


@router.get(Base + "/{run_id}/raw")
def raw_artifact(
    document_id: UUID,
    version_id: UUID,
    run_id: UUID,
    actor: Actor,
    service: Service,
) -> Response:
    """The immutable structured parser output for this run, for debugging and reproduction."""
    actor.require("document:manage")
    with service.sessions() as session:
        run = _resolve(session, actor.tenant_id, document_id, version_id, run_id)
        if not run.raw_artifact_key or not run.raw_artifact_version_id:
            raise DomainError(
                "RAW_ARTIFACT_UNAVAILABLE", "No raw parse artifact exists for this run.", 404
            )
        key, object_version = run.raw_artifact_key, run.raw_artifact_version_id
    return _stream(service, key, object_version, "application/json")


def _stream(service: Any, key: str, object_version: str, media_type: str) -> Response:
    try:
        body = service.storage.open(key, object_version)
    except Exception:
        raise DomainError("STORAGE_FAILURE", "The stored artifact is unavailable.", 503) from None

    def chunks() -> Any:
        try:
            while chunk := body.read(service.settings.ingestion.stream_chunk_bytes):
                yield chunk
        finally:
            body.close()

    return StreamingResponse(
        chunks(),
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
