import asyncio
import base64
import hashlib
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app.core.errors import DomainError
from app.ingestion.chunking.errors import retryable
from app.ingestion.validation.files import sanitize_filename, validate_mime, validate_pdf
from app.models.documents import (
    AuditEvent,
    Document,
    DocumentVersion,
    IngestionJob,
    IngestionStageEvent,
)
from app.models.enums import SourceType, Status
from app.observability.ingestion import audit, phase_event
from app.repositories.documents import ensure_actor, get_document, get_job, get_version
from app.schemas.documents import (
    AuditView,
    DocumentMetadata,
    DocumentView,
    JobView,
    Page,
    StageView,
    UploadMetadata,
    UploadResult,
    VersionMetadata,
    VersionView,
)
from app.security.auth import Principal
from app.services.control import ControlPlane

router = APIRouter(prefix="/api/v1")


def control(request: Request) -> ControlPlane:
    service: ControlPlane | None = request.app.state.control
    if service is None:
        raise DomainError("SERVICE_NOT_CONFIGURED", "The document service is not configured.", 503)
    return service


def principal(request: Request, service: Annotated[ControlPlane, Depends(control)]) -> Principal:
    actor = service.auth.authenticate(request.headers.get("Authorization"))
    with service.sessions.begin() as session:
        ensure_actor(session, actor)
    return actor


Actor = Annotated[Principal, Depends(principal)]
Service = Annotated[ControlPlane, Depends(control)]


def correlation(request: Request) -> UUID:
    return UUID(request.state.request_id)


@router.get("/auth/me")
def me(actor: Actor) -> dict[str, Any]:
    return {
        "user_id": actor.user_id,
        "tenant_id": actor.tenant_id,
        "display_name": actor.display_name,
        "role": actor.role,
        "permissions": sorted(actor.permissions),
        "auth_mode": "development",
        "production_ready": False,
    }


def parse_metadata(encoded: str | None, schema: type[VersionMetadata]) -> VersionMetadata:
    if not encoded or len(encoded) > 12000:
        raise DomainError(
            "INVALID_METADATA", "Upload metadata is required and must fit the header limit.", 422
        )
    try:
        return schema.model_validate_json(base64.b64decode(encoded, validate=True))
    except (ValueError, ValidationError):
        raise DomainError(
            "INVALID_METADATA",
            "Check the filename, title, source type, authority, and edition metadata.",
            422,
        ) from None


async def upload(
    request: Request, actor: Principal, service: ControlPlane, document_id: UUID | None
) -> UploadResult:
    service.metrics.uploads.inc()
    config = service.settings.ingestion
    request_id = correlation(request)
    try:
        actor.require("document:upload")
        try:
            key = UUID(request.headers.get("Idempotency-Key", ""))
        except ValueError:
            raise DomainError(
                "INVALID_IDEMPOTENCY_KEY", "A UUID Idempotency-Key is required.", 422
            ) from None
        meta = parse_metadata(
            request.headers.get("X-Upload-Metadata"),
            VersionMetadata if document_id else UploadMetadata,
        )
        normalized = sanitize_filename(meta.filename)
        validate_mime(request.headers.get("Content-Type"), config)
        length = request.headers.get("Content-Length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                raise DomainError("UPLOAD_INVALID_LENGTH", "Invalid upload length.") from None
            if size > config.max_upload_bytes:
                raise DomainError(
                    "UPLOAD_FILE_TOO_LARGE", "The PDF exceeds the upload size limit.", 413
                )
            if size <= 0:
                raise DomainError("UPLOAD_EMPTY", "Choose a nonempty PDF.")
        # Authorization for an existing publication precedes receiving its file bytes.
        if document_id:

            def check_scope() -> None:
                with service.sessions() as session:
                    doc = get_document(session, actor.tenant_id, document_id)
                    if doc.archived_at:
                        raise DomainError("DOCUMENT_ARCHIVED", "This document is archived.", 409)

            await run_in_threadpool(check_scope)
        # Disk spool and hashing are bounded even when Content-Length is absent or misleading.
        with tempfile.TemporaryDirectory(prefix="medrag-upload-") as directory:
            path = Path(directory) / "upload.pdf"
            digest = hashlib.sha256()
            received = 0
            try:
                async with asyncio.timeout(config.upload_timeout_seconds):
                    with path.open("wb") as output:
                        async for incoming in request.stream():
                            received += len(incoming)
                            if received > config.max_upload_bytes:
                                raise DomainError(
                                    "UPLOAD_FILE_TOO_LARGE",
                                    "The PDF exceeds the upload size limit.",
                                    413,
                                )
                            for start in range(0, len(incoming), config.stream_chunk_bytes):
                                block = incoming[start : start + config.stream_chunk_bytes]
                                digest.update(block)
                                await run_in_threadpool(output.write, block)
            except TimeoutError:
                raise DomainError(
                    "UPLOAD_TIMEOUT", "The upload exceeded its time limit.", 408
                ) from None
            if received == 0:
                raise DomainError("UPLOAD_EMPTY", "Choose a nonempty PDF.")
            if length is not None and received != int(length):
                raise DomainError(
                    "UPLOAD_INVALID_LENGTH", "The received file length does not match."
                )
            phase_event("UPLOAD_CHECKSUM_COMPLETED", request_id)
            await run_in_threadpool(validate_pdf, path, config)
            phase_event("UPLOAD_VALIDATION_COMPLETED", request_id)
            publication = meta.document if isinstance(meta, UploadMetadata) else None
            version_metadata = VersionMetadata.model_validate(meta.model_dump(exclude={"document"}))
            return await run_in_threadpool(
                service.uploads.accept,
                actor,
                key,
                request_id,
                path,
                digest.hexdigest(),
                normalized,
                version_metadata,
                publication,
                document_id,
            )
    except DomainError as exc:
        await run_in_threadpool(service.uploads.reject, actor, request_id, exc)
        raise


@router.post("/documents", response_model=UploadResult, status_code=201)
async def create_document(request: Request, actor: Actor, service: Service) -> UploadResult:
    """Raw PDF body; X-Upload-Metadata is base64-encoded UTF-8 UploadMetadata JSON."""
    return await upload(request, actor, service, None)


@router.post("/documents/{document_id}/versions", response_model=UploadResult, status_code=201)
async def create_version(
    document_id: UUID, request: Request, actor: Actor, service: Service
) -> UploadResult:
    """Raw PDF body; X-Upload-Metadata contains VersionMetadata, not publication metadata."""
    return await upload(request, actor, service, document_id)


def document_view(session: Any, document: Document) -> DocumentView:
    result = DocumentView.model_validate(document)
    latest = session.scalar(
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.tenant_id == document.tenant_id,
        )
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
    )
    if latest:
        result.latest_version = VersionView.model_validate(latest)
    return result


@router.get("/documents", response_model=Page[DocumentView])
def documents(
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    source_type: SourceType | None = None,
    subject: str | None = None,
    specialty: str | None = None,
    archived: bool = False,
) -> Page[DocumentView]:
    actor.require("document:read")
    query = select(Document).where(Document.tenant_id == actor.tenant_id)
    query = query.where(
        Document.archived_at.is_not(None) if archived else Document.archived_at.is_(None)
    )
    for column, value in (
        (Document.source_type, source_type),
        (Document.subject, subject),
        (Document.specialty, specialty),
    ):
        if value is not None:
            query = query.where(column == value)
    with service.sessions() as session:
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(Document.created_at.desc(), Document.id).offset(offset).limit(limit)
        )
        return Page(
            items=[document_view(session, row) for row in rows],
            total=total,
            offset=offset,
            limit=limit,
        )


@router.get("/documents/{document_id}", response_model=DocumentView)
def document(document_id: UUID, actor: Actor, service: Service) -> DocumentView:
    actor.require("document:read")
    with service.sessions() as session:
        return document_view(session, get_document(session, actor.tenant_id, document_id))


@router.patch("/documents/{document_id}", response_model=DocumentView)
def update_document(
    document_id: UUID, metadata: DocumentMetadata, request: Request, actor: Actor, service: Service
) -> DocumentView:
    actor.require("document:manage")
    with service.sessions.begin() as session:
        doc = get_document(session, actor.tenant_id, document_id, lock=True)
        if doc.archived_at:
            raise DomainError("DOCUMENT_ARCHIVED", "Archived metadata cannot be changed.", 409)
        for key, value in metadata.model_dump().items():
            setattr(doc, key, value)
        audit(
            session,
            actor.tenant_id,
            actor.user_id,
            "DOCUMENT_UPDATED",
            doc.id,
            correlation(request),
            {"fields": sorted(metadata.model_fields_set)},
        )
        session.flush()
        return document_view(session, doc)


@router.post("/documents/{document_id}/archive", status_code=204)
def archive(document_id: UUID, request: Request, actor: Actor, service: Service) -> None:
    service.jobs.archive(actor, document_id, correlation(request))


@router.get("/documents/{document_id}/versions", response_model=Page[VersionView])
def versions(
    document_id: UUID,
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Page[VersionView]:
    actor.require("document:read")
    with service.sessions() as session:
        get_document(session, actor.tenant_id, document_id)
        query = select(DocumentVersion).where(
            DocumentVersion.tenant_id == actor.tenant_id, DocumentVersion.document_id == document_id
        )
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(DocumentVersion.version_number.desc()).offset(offset).limit(limit)
        )
        return Page(
            items=[VersionView.model_validate(row) for row in rows],
            total=total,
            offset=offset,
            limit=limit,
        )


@router.get("/documents/{document_id}/versions/{version_id}/source")
def source(
    document_id: UUID, version_id: UUID, actor: Actor, service: Service
) -> StreamingResponse:
    actor.require("document:read")
    with service.sessions() as session:
        get_document(session, actor.tenant_id, document_id)
        version = get_version(session, actor.tenant_id, version_id)
        if version.document_id != document_id:
            raise DomainError("DOCUMENT_VERSION_NOT_FOUND", "Document version not found.", 404)
        try:
            body = service.storage.open(version.object_storage_key, version.object_version_id)
        except Exception:
            raise DomainError("STORAGE_FAILURE", "The original file is unavailable.", 503) from None

    def stream() -> Any:
        try:
            while chunk := body.read(service.settings.ingestion.stream_chunk_bytes):
                yield chunk
        finally:
            body.close()

    return StreamingResponse(
        stream(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{version.normalized_filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def job_view(session: Any, job: IngestionJob, history: bool = False) -> JobView:
    result = JobView.model_validate(job)
    version = get_version(session, job.tenant_id, job.document_version_id)
    doc = get_document(session, job.tenant_id, version.document_id)
    result.document_id, result.document_title = doc.id, doc.title
    result.version_number = version.version_number
    if history:
        result.events = [
            StageView.model_validate(event)
            for event in session.scalars(
                select(IngestionStageEvent)
                .where(IngestionStageEvent.ingestion_job_id == job.id)
                .order_by(IngestionStageEvent.sequence)
            )
        ]
    return result


@router.get("/ingestion/jobs", response_model=Page[JobView])
def jobs(
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status: Status | None = None,
    document_id: UUID | None = None,
    since: datetime | None = None,
    active: bool | None = None,
) -> Page[JobView]:
    actor.require("ingestion:read")
    query = select(IngestionJob).where(IngestionJob.tenant_id == actor.tenant_id)
    if status:
        query = query.where(IngestionJob.status == status)
    if document_id:
        query = query.join(
            DocumentVersion, DocumentVersion.id == IngestionJob.document_version_id
        ).where(DocumentVersion.document_id == document_id)
    if since:
        query = query.where(IngestionJob.created_at >= since)
    if active is not None:
        query = query.where(
            IngestionJob.status.in_([Status.UPLOADED, Status.VALIDATING, Status.QUEUED])
            if active
            else IngestionJob.status.in_(
                [Status.FAILED, Status.QUARANTINED, Status.NEEDS_REVIEW, Status.CANCELLED]
            )
        )
    with service.sessions() as session:
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(IngestionJob.created_at.desc(), IngestionJob.id)
            .offset(offset)
            .limit(limit)
        )
        return Page(
            items=[job_view(session, row) for row in rows], total=total, offset=offset, limit=limit
        )


@router.get("/ingestion/jobs/{job_id}", response_model=JobView)
def job(job_id: UUID, actor: Actor, service: Service) -> JobView:
    actor.require("ingestion:read")
    with service.sessions() as session:
        return job_view(session, get_job(session, actor.tenant_id, job_id), history=True)


@router.post("/ingestion/jobs/{job_id}/retry", response_model=JobView)
def retry(job_id: UUID, request: Request, actor: Actor, service: Service) -> JobView:
    actor.require("ingestion:retry")
    with service.sessions() as session:
        current = get_job(session, actor.tenant_id, job_id)
        chunk_failure = bool(
            current.last_error_code and current.last_error_code.startswith("CHUNK_")
        )
        code = current.last_error_code
    if chunk_failure:
        if not retryable(code):
            raise DomainError(
                "CHUNK_RETRY_NOT_ALLOWED",
                "Correct the source or policy and request rechunking.",
                409,
            )
        service.chunks.request(actor, job_id, correlation(request), force=True)
    else:
        service.jobs.change(actor, job_id, "retry", correlation(request))
    return job(job_id, actor, service)


@router.post("/ingestion/jobs/{job_id}/reparse", response_model=JobView)
def reparse(job_id: UUID, request: Request, actor: Actor, service: Service) -> JobView:
    """Explicitly reprocess an already parsed, flagged or failed version.

    Nothing reparses on its own. The existing active parse stays active until a new run
    succeeds, and an unchanged parser build plus unchanged policy plus unchanged bytes is a
    no-op rather than a duplicate dataset.
    """
    service.jobs.change(actor, job_id, "reparse", correlation(request))
    return job(job_id, actor, service)


@router.post("/ingestion/jobs/{job_id}/cancel", response_model=JobView)
def cancel(job_id: UUID, request: Request, actor: Actor, service: Service) -> JobView:
    service.jobs.change(actor, job_id, "cancel", correlation(request))
    return job(job_id, actor, service)


@router.get("/audit", response_model=Page[AuditView])
def audit_events(
    actor: Actor,
    service: Service,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> Page[AuditView]:
    actor.require("audit:read")
    query = select(AuditEvent).where(AuditEvent.tenant_id == actor.tenant_id)
    with service.sessions() as session:
        total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
        rows = session.scalars(
            query.order_by(AuditEvent.created_at.desc(), AuditEvent.id).offset(offset).limit(limit)
        )
        return Page(
            items=[AuditView.model_validate(row) for row in rows],
            total=total,
            offset=offset,
            limit=limit,
        )
