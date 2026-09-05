from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import DomainError
from app.ingestion.state import transition
from app.models.documents import DocumentVersion, IngestionJob, OutboxMessage
from app.models.enums import Status
from app.observability.ingestion import audit
from app.repositories.documents import get_document, get_job, get_version
from app.security.auth import Principal
from app.services.storage import ObjectStorage


class JobService:
    def __init__(self, sessions: sessionmaker[Session], storage: ObjectStorage) -> None:
        self.sessions, self.storage = sessions, storage

    def change(self, actor: Principal, job_id: UUID, action: str, correlation_id: UUID) -> None:
        actor.require(f"ingestion:{action}")
        with self.sessions.begin() as session:
            existing = get_job(session, actor.tenant_id, job_id)
            version = get_version(session, actor.tenant_id, existing.document_version_id)
            document = get_document(session, actor.tenant_id, version.document_id, lock=True)
            job = get_job(session, actor.tenant_id, job_id, lock=True)
            session.refresh(job)
            job.correlation_id = correlation_id
            if action == "cancel":
                if job.status == Status.CANCELLED:
                    return  # Idempotent cancellation, no duplicate history.
                transition(session, job, version, Status.CANCELLED, actor.user_id)
                audit(
                    session,
                    actor.tenant_id,
                    actor.user_id,
                    "INGESTION_CANCELLED",
                    job.id,
                    correlation_id,
                )
                return
            if document.archived_at:
                raise DomainError(
                    "DOCUMENT_ARCHIVED", "Archived document jobs cannot be retried.", 409
                )
            transition(session, job, version, Status.VALIDATING, actor.user_id, retry=True)
            audit(
                session, actor.tenant_id, actor.user_id, "INGESTION_RETRIED", job.id, correlation_id
            )
            try:
                stored = self.storage.stat(version.object_storage_key, version.object_version_id)
            except Exception:
                transition(
                    session,
                    job,
                    version,
                    Status.FAILED,
                    actor.user_id,
                    error_code="STORAGE_FAILURE",
                    error_message="The original file is unavailable.",
                )
                return
            if stored.size != version.file_size_bytes or stored.sha256 != version.sha256:
                transition(
                    session,
                    job,
                    version,
                    Status.QUARANTINED,
                    actor.user_id,
                    error_code="STORAGE_INTEGRITY_FAILURE",
                    error_message="The original file failed integrity verification.",
                )
                return
            transition(session, job, version, Status.QUEUED, actor.user_id)
            session.add(OutboxMessage(job_id=job.id, generation=job.retry_count))

    def archive(self, actor: Principal, document_id: UUID, correlation_id: UUID) -> None:
        actor.require("document:manage")
        with self.sessions.begin() as session:
            document = get_document(session, actor.tenant_id, document_id, lock=True)
            if document.archived_at:
                return
            document.archived_at = datetime.now(UTC)
            versions = list(
                session.scalars(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == document_id,
                        DocumentVersion.tenant_id == actor.tenant_id,
                    )
                )
            )
            for version in versions:
                version.archived_at = document.archived_at
                job = session.scalar(
                    select(IngestionJob)
                    .where(IngestionJob.document_version_id == version.id)
                    .with_for_update()
                )
                if job and job.status != Status.CANCELLED:
                    job.correlation_id = correlation_id
                    transition(session, job, version, Status.CANCELLED, actor.user_id)
                    audit(
                        session,
                        actor.tenant_id,
                        actor.user_id,
                        "INGESTION_CANCELLED",
                        job.id,
                        correlation_id,
                        {"reason": "document_archived"},
                    )
            audit(
                session,
                actor.tenant_id,
                actor.user_id,
                "DOCUMENT_ARCHIVED",
                document_id,
                correlation_id,
            )
