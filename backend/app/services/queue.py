from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.ingestion_config import IngestionConfig
from app.ingestion.state import transition
from app.models.documents import Document, DocumentVersion, IngestionJob, OutboxMessage
from app.models.enums import Status
from app.observability.ingestion import audit, phase_event
from app.services.storage import ObjectStorage


class QueuePublisher(Protocol):
    def publish(self, message_id: UUID) -> None: ...


# Each outbox message kind is only meaningful while its job sits in the state that produced it.
# A delivery that arrives after the job moved on is stale and must be discarded, never replayed.
EXPECTED_STATUS = {
    "PARSING": Status.QUEUED,
    "CHUNKING": Status.READY_FOR_CHUNKING,
    "EMBEDDING": Status.READY_FOR_EMBEDDING,
    "SPARSE_INDEX": Status.READY_FOR_RETRIEVAL,
}


def expected_status(kind: str | None) -> Status:
    return EXPECTED_STATUS.get(kind or "PARSING", Status.QUEUED)


def dispatch(
    sessions: sessionmaker[Session], publisher: QueuePublisher, config: IngestionConfig
) -> int:
    sent = 0
    with sessions.begin() as session:
        due = datetime.now(UTC) - timedelta(seconds=config.delivery_retry_seconds)
        messages = list(
            session.scalars(
                select(OutboxMessage)
                .where(
                    OutboxMessage.received_at.is_(None),
                    or_(OutboxMessage.published_at.is_(None), OutboxMessage.published_at < due),
                )
                .order_by(OutboxMessage.created_at)
                .with_for_update(skip_locked=True)
                .limit(config.dispatch_batch_size)
            )
        )
        for message in messages:
            job = session.get(IngestionJob, message.job_id)
            if (
                job is None
                or job.status != expected_status(message.kind)
                or job.retry_count != message.generation
            ):
                message.received_at = datetime.now(
                    UTC
                )  # Obsolete, never resume a cancelled generation.
                continue
            message.attempts += 1
            message.published_at = datetime.now(UTC)  # Also bounds failure retry frequency.
            try:
                publisher.publish(message.id)
                message.last_error_code = None
                sent += 1
                phase_event("INGESTION_QUEUE_PUBLISHED", job.correlation_id, job.id)
            except Exception:
                message.last_error_code = "QUEUE_UNAVAILABLE"
                phase_event("INGESTION_QUEUE_UNAVAILABLE", job.correlation_id, job.id)
    return sent


def receive(
    sessions: sessionmaker[Session], storage: ObjectStorage, message_id: UUID
) -> UUID | None:
    """Confirm durable receipt and report the job that is now eligible to be parsed.

    Returns the job id only for the single delivery that legitimately claimed this message.
    Every duplicate, stale or cancelled delivery returns None, so redelivery can never start a
    second parse for the same job generation.
    """
    with sessions.begin() as session:
        message = session.get(OutboxMessage, message_id)
        if message is None:
            return None
        job = session.get(IngestionJob, message.job_id)
        if job is None:
            return None
        version = session.get(DocumentVersion, job.document_version_id)
        if version is None:
            return None
        # Consistent document -> job -> outbox lock order with cancel/archive paths.
        document = session.scalar(
            select(Document).where(Document.id == version.document_id).with_for_update()
        )
        session.refresh(job, with_for_update=True)
        session.refresh(message, with_for_update=True)
        if message.received_at:
            return None
        message.received_at = datetime.now(UTC)
        if (
            document is None
            or document.archived_at
            or job.status != expected_status(message.kind)
            or (job.retry_count != message.generation)
        ):
            return None
        try:
            stored = storage.stat(version.object_storage_key, version.object_version_id)
        except Exception:
            transition(
                session,
                job,
                version,
                Status.FAILED,
                None,
                service="celery-receipt",
                error_code="STORAGE_FAILURE",
                error_message="The original file is unavailable.",
            )
            return None
        if stored.size != version.file_size_bytes or stored.sha256 != version.sha256:
            transition(
                session,
                job,
                version,
                Status.QUARANTINED,
                None,
                service="celery-receipt",
                error_code="STORAGE_INTEGRITY_FAILURE",
                error_message="The original file failed integrity verification.",
            )
            return None
        job.queue_received_at = datetime.now(UTC)
        audit(
            session,
            job.tenant_id,
            None,
            "INGESTION_QUEUE_RECEIVED",
            job.id,
            job.correlation_id,
            {"generation": message.generation},
        )
        # Receipt itself never parses, activates or indexes. It only confirms that this job
        # generation is durably owned by this worker, which may then run the M2 parse pipeline.
        return job.id
