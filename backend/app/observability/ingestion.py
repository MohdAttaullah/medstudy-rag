import logging
from uuid import UUID

from prometheus_client import CollectorRegistry, Counter
from sqlalchemy.orm import Session

from app.models.documents import AuditEvent

logger = logging.getLogger("medical_rag.requests")


def phase_event(event: str, correlation_id: UUID, resource_id: UUID | None = None) -> None:
    logger.info(
        event,
        extra={
            "event": event,
            "request_id": str(correlation_id),
            "resource_id": str(resource_id) if resource_id else None,
        },
    )


def audit(
    session: Session,
    tenant_id: UUID,
    actor_id: UUID | None,
    event: str,
    resource_id: UUID | None,
    correlation_id: UUID,
    metadata: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=actor_id,
            event_type=event,
            resource_id=resource_id,
            correlation_id=correlation_id,
            safe_metadata=metadata or {},
        )
    )
    logger.info(
        event,
        extra={
            "event": event,
            "request_id": str(correlation_id),
            "resource_id": str(resource_id) if resource_id else None,
        },
    )


class IngestionMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.uploads = Counter("uploads_total", "Upload requests", registry=registry)
        self.rejected = Counter("uploads_rejected_total", "Rejected uploads", registry=registry)
        self.bytes = Counter("upload_bytes_total", "Accepted upload bytes", registry=registry)
        self.jobs = Counter(
            "ingestion_jobs_created_total", "Committed ingestion jobs", registry=registry
        )
        self.storage_failures = Counter(
            "storage_operation_failures_total", "Storage failures", registry=registry
        )
        self.duplicates = Counter("duplicate_uploads_total", "Duplicate files", registry=registry)
