import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import DomainError
from app.core.ingestion_config import IngestionConfig
from app.ingestion.state import record_initial, transition
from app.models.documents import (
    Document,
    DocumentVersion,
    IngestionJob,
    OutboxMessage,
    Tenant,
    UploadIntent,
)
from app.models.enums import Status
from app.observability.ingestion import IngestionMetrics, audit, phase_event
from app.repositories.documents import ensure_actor, get_document
from app.schemas.documents import DocumentMetadata, UploadResult, VersionMetadata
from app.security.auth import Principal
from app.services.deletion import document_is_processing
from app.services.storage import ObjectStorage, object_key


class UploadService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        storage: ObjectStorage,
        config: IngestionConfig,
        metrics: IngestionMetrics,
    ) -> None:
        self.sessions, self.storage, self.config, self.metrics = sessions, storage, config, metrics

    def reject(self, actor: Principal, correlation_id: UUID, error: DomainError) -> None:
        self.metrics.rejected.inc()
        if error.code == "UPLOAD_DUPLICATE":
            self.metrics.duplicates.inc()
        with self.sessions.begin() as session:
            ensure_actor(session, actor)
            audit(
                session,
                actor.tenant_id,
                actor.user_id,
                "DUPLICATE_DETECTED" if error.code == "UPLOAD_DUPLICATE" else "UPLOAD_REJECTED",
                None,
                correlation_id,
                {"error_code": error.code},
            )

    @staticmethod
    def _result(session: Session, intent: UploadIntent, replayed: bool) -> UploadResult:
        job = session.get(IngestionJob, intent.job_id)
        if job is None:
            raise DomainError("PERSISTENCE_FAILURE", "Upload records are unavailable.", 503)
        return UploadResult(
            document_id=intent.document_id,
            version_id=intent.version_id,
            job_id=intent.job_id,
            status=job.status,
            replayed=replayed,
        )

    def accept(
        self,
        actor: Principal,
        key: UUID,
        correlation_id: UUID,
        path: Path,
        sha256: str,
        normalized: str,
        metadata: VersionMetadata,
        document_metadata: DocumentMetadata | None,
        document_id: UUID | None,
    ) -> UploadResult:
        actor.require("document:upload")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "sha256": sha256,
                    "version": metadata.model_dump(mode="json"),
                    "document": document_metadata.model_dump(mode="json")
                    if document_metadata
                    else None,
                    "document_id": str(document_id) if document_id else None,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            ensure_actor(session, actor)
            # Short reservation transaction serializes deduplication within the authorization scope.
            session.scalar(select(Tenant).where(Tenant.id == actor.tenant_id).with_for_update())
            if document_id is not None:
                doc = get_document(session, actor.tenant_id, document_id)
                if doc.archived_at:
                    raise DomainError(
                        "DOCUMENT_ARCHIVED", "Archived documents cannot receive versions.", 409
                    )
                if document_is_processing(session, doc):
                    raise DomainError(
                        "DOCUMENT_PROCESSING",
                        "This document is still being processed. Add a new version when it "
                        "finishes.",
                        409,
                    )
            old = session.scalar(
                select(UploadIntent).where(
                    UploadIntent.tenant_id == actor.tenant_id,
                    UploadIntent.actor_id == actor.user_id,
                    UploadIntent.idempotency_key == key,
                )
            )
            if old:
                if old.fingerprint != fingerprint:
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT",
                        "This request key was used for a different upload.",
                        409,
                    )
                if old.state == "COMPLETE":
                    return self._result(session, old, True)
                if old.state == "FAILED":
                    raise DomainError(
                        old.error_code or "UPLOAD_FAILED",
                        "This upload attempt failed. Retry to start a new attempt.",
                        409,
                        {"retry_with_new_key": "true"},
                    )
                raise DomainError(
                    "UPLOAD_IN_PROGRESS", "This upload is still being reconciled. Retry later.", 409
                )
            duplicate = session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.tenant_id == actor.tenant_id, DocumentVersion.sha256 == sha256
                )
            )
            if duplicate:
                raise DomainError(
                    "UPLOAD_DUPLICATE",
                    "This exact file already exists in your workspace.",
                    409,
                    {"document_id": str(duplicate.document_id), "version_id": str(duplicate.id)},
                )
            pending = session.scalar(
                select(UploadIntent.id).where(
                    UploadIntent.tenant_id == actor.tenant_id,
                    UploadIntent.sha256 == sha256,
                    UploadIntent.state == "PENDING",
                )
            )
            if pending:
                raise DomainError(
                    "UPLOAD_IN_PROGRESS", "An identical file is currently being uploaded.", 409
                )
            doc_id, version_id = document_id or uuid4(), uuid4()
            intent = UploadIntent(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                actor_id=actor.user_id,
                idempotency_key=key,
                fingerprint=fingerprint,
                sha256=sha256,
                document_id=doc_id,
                version_id=version_id,
                job_id=uuid4(),
                object_key=object_key(doc_id, version_id),
                state="PENDING",
                expires_at=datetime.now(UTC) + timedelta(seconds=self.config.intent_expiry_seconds),
                correlation_id=correlation_id,
                cleanup_required=False,
            )
            session.add(intent)
            session.flush()
            intent_id = intent.id

        phase = "STORAGE_FAILURE"
        try:
            # Lock fences the reconciler during upload/finalization; it uses SKIP LOCKED.
            with self.sessions.begin() as session:
                current = session.scalar(
                    select(UploadIntent).where(UploadIntent.id == intent_id).with_for_update()
                )
                if current is None or current.state != "PENDING":
                    raise DomainError("UPLOAD_EXPIRED", "The upload reservation expired.", 409)
                with path.open("rb") as stream:
                    stored = self.storage.put(current.object_key, stream, sha256)
                if stored.size != path.stat().st_size or stored.sha256 != sha256:
                    raise DomainError(
                        "STORAGE_FAILURE", "The stored file could not be verified.", 503
                    )
                phase_event("UPLOAD_STORAGE_VERIFIED", correlation_id, current.version_id)
                phase = "PERSISTENCE_FAILURE"
                # Reserve version numbers under the publication row lock.
                if document_id:
                    document = get_document(session, actor.tenant_id, document_id, lock=True)
                    if document.archived_at:
                        raise DomainError(
                            "DOCUMENT_ARCHIVED", "The document was archived during upload.", 409
                        )
                else:
                    if document_metadata is None:
                        raise DomainError("INVALID_METADATA", "Publication metadata is required.")
                    document = Document(
                        id=current.document_id,
                        tenant_id=actor.tenant_id,
                        created_by_user_id=actor.user_id,
                        **document_metadata.model_dump(),
                    )
                    session.add(document)
                    session.flush()
                    audit(
                        session,
                        actor.tenant_id,
                        actor.user_id,
                        "DOCUMENT_CREATED",
                        document.id,
                        correlation_id,
                    )
                number = (
                    int(
                        session.scalar(
                            select(
                                func.coalesce(func.max(DocumentVersion.version_number), 0)
                            ).where(DocumentVersion.document_id == document.id)
                        )
                        or 0
                    )
                    + 1
                )
                version = DocumentVersion(
                    id=current.version_id,
                    tenant_id=actor.tenant_id,
                    document_id=document.id,
                    version_number=number,
                    edition=metadata.edition,
                    publication_year=metadata.publication_year,
                    original_filename=metadata.filename,
                    normalized_filename=normalized,
                    mime_type="application/pdf",
                    file_size_bytes=stored.size,
                    sha256=sha256,
                    object_storage_key=current.object_key,
                    object_version_id=stored.version_id,
                    ingestion_status=Status.UPLOADED,
                    searchable=False,
                    created_by_user_id=actor.user_id,
                )
                session.add(version)
                session.flush()
                job = IngestionJob(
                    id=current.job_id,
                    tenant_id=actor.tenant_id,
                    document_version_id=version.id,
                    status=Status.UPLOADED,
                    current_stage="UPLOADED",
                    requested_by_user_id=actor.user_id,
                    configuration_version=self.config.version,
                    config_snapshot=self.config.model_dump(mode="json"),
                    correlation_id=correlation_id,
                    retry_count=0,
                    max_retries=self.config.max_retries,
                )
                session.add(job)
                session.flush()
                audit(
                    session,
                    actor.tenant_id,
                    actor.user_id,
                    "DOCUMENT_VERSION_UPLOADED",
                    version.id,
                    correlation_id,
                    {"file_size_bytes": stored.size},
                )
                audit(
                    session,
                    actor.tenant_id,
                    actor.user_id,
                    "INGESTION_JOB_CREATED",
                    job.id,
                    correlation_id,
                )
                record_initial(session, job, actor.user_id)
                transition(session, job, version, Status.VALIDATING, actor.user_id)
                transition(session, job, version, Status.QUEUED, actor.user_id)
                session.add(OutboxMessage(job_id=job.id, generation=0))
                current.state = "COMPLETE"
                result = self._result(session, current, False)
            phase_event("UPLOAD_COMMITTED", correlation_id, result.job_id)
            self.metrics.jobs.inc()
            self.metrics.bytes.inc(stored.size)
            return result
        except Exception as exc:
            # Resolve ambiguous commits in PostgreSQL before deleting an uncommitted object.
            code = exc.code if isinstance(exc, DomainError) else phase
            if phase == "STORAGE_FAILURE":
                self.metrics.storage_failures.inc()
            recovered = self.compensate(intent_id, code)
            if recovered is not None:
                return recovered
            if isinstance(exc, DomainError):
                exc.details["retry_with_new_key"] = "true"
                raise
            raise DomainError(
                code,
                "The upload could not be completed. Its storage reservation will be reconciled.",
                503,
                {"retry_with_new_key": "true"},
            ) from None

    def compensate(self, intent_id: UUID, error_code: str) -> UploadResult | None:
        with self.sessions.begin() as session:
            intent = session.scalar(
                select(UploadIntent).where(UploadIntent.id == intent_id).with_for_update()
            )
            if intent is None:
                return None
            if intent.state == "COMPLETE":
                return self._result(session, intent, True)
            if session.scalar(
                select(DocumentVersion.id).where(
                    DocumentVersion.object_storage_key == intent.object_key
                )
            ):
                raise DomainError(
                    "PERSISTENCE_FAILURE", "Upload reconciliation requires review.", 503
                )
            intent.state, intent.error_code = "FAILED", error_code
            intent.cleanup_required = True
            try:
                self.storage.delete(intent.object_key)
                intent.cleanup_required = False
            except Exception:
                self.metrics.storage_failures.inc()
            audit(
                session,
                intent.tenant_id,
                intent.actor_id,
                "UPLOAD_REJECTED",
                intent.id,
                intent.correlation_id,
                {"error_code": error_code, "cleanup_pending": intent.cleanup_required},
            )
        return None

    def recover(self) -> int:
        count = 0
        # Each key is fenced independently, so active uploads are not blocked by the sweep.
        with self.sessions.begin() as session:
            intents = list(
                session.scalars(
                    select(UploadIntent)
                    .where(
                        (
                            (UploadIntent.state == "PENDING")
                            & (UploadIntent.expires_at < datetime.now(UTC))
                        )
                        | ((UploadIntent.state == "FAILED") & UploadIntent.cleanup_required)
                    )
                    .with_for_update(skip_locked=True)
                    .limit(self.config.dispatch_batch_size)
                )
            )
            for intent in intents:
                if session.scalar(
                    select(DocumentVersion.id).where(
                        DocumentVersion.object_storage_key == intent.object_key
                    )
                ):
                    continue
                intent.state = "FAILED"
                intent.error_code = intent.error_code or "UPLOAD_ABANDONED"
                intent.cleanup_required = True
                try:
                    self.storage.delete(intent.object_key)
                    intent.cleanup_required = False
                    count += 1
                except Exception:
                    self.metrics.storage_failures.inc()
                audit(
                    session,
                    intent.tenant_id,
                    intent.actor_id,
                    "UPLOAD_RECONCILED",
                    intent.id,
                    intent.correlation_id,
                    {"cleanup_pending": intent.cleanup_required},
                )
        return count
