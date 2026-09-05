from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models.documents import Document, DocumentVersion, IngestionJob, Tenant, User
from app.security.auth import Principal


def ensure_actor(session: Session, principal: Principal) -> None:
    session.execute(
        insert(Tenant)
        .values(id=principal.tenant_id, name="Development workspace")
        .on_conflict_do_nothing(index_elements=["id"])
    )
    session.execute(
        insert(User)
        .values(
            id=principal.user_id,
            tenant_id=principal.tenant_id,
            display_name=principal.display_name,
            role=principal.role,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    user = session.get(User, principal.user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise DomainError("FORBIDDEN", "Identity scope does not match.", 403)


def get_document(
    session: Session, tenant_id: UUID, document_id: UUID, lock: bool = False
) -> Document:
    query = select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    document = session.scalar(query.with_for_update() if lock else query)
    if document is None:
        raise DomainError("DOCUMENT_NOT_FOUND", "Document not found.", 404)
    return document


def get_version(session: Session, tenant_id: UUID, version_id: UUID) -> DocumentVersion:
    version = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == version_id, DocumentVersion.tenant_id == tenant_id
        )
    )
    if version is None:
        raise DomainError("DOCUMENT_VERSION_NOT_FOUND", "Document version not found.", 404)
    return version


def get_job(session: Session, tenant_id: UUID, job_id: UUID, lock: bool = False) -> IngestionJob:
    query = select(IngestionJob).where(
        IngestionJob.id == job_id, IngestionJob.tenant_id == tenant_id
    )
    job = session.scalar(query.with_for_update() if lock else query)
    if job is None:
        raise DomainError("INGESTION_JOB_NOT_FOUND", "Ingestion job not found.", 404)
    return job
