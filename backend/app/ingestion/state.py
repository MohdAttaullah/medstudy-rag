from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models.documents import DocumentVersion, IngestionJob, IngestionStageEvent
from app.models.enums import Status
from app.observability.ingestion import audit

# M3+ states (CHUNKING, EMBEDDING, INDEXING, VERIFYING_INDEX, READY) are intentionally absent
# from the executable transition graph. M2 ends at READY_FOR_CHUNKING, which is terminal here.
FAILURE_STATES = frozenset(
    {Status.FAILED, Status.QUARANTINED, Status.NEEDS_REVIEW, Status.CANCELLED}
)
PARSE_STAGES = (Status.PARSING, Status.NORMALIZING, Status.ENRICHING)
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.UPLOADED: frozenset({Status.VALIDATING, Status.CANCELLED}),
    Status.VALIDATING: frozenset(
        {Status.QUEUED, Status.FAILED, Status.QUARANTINED, Status.NEEDS_REVIEW, Status.CANCELLED}
    ),
    Status.QUEUED: frozenset({Status.PARSING, *FAILURE_STATES} - {Status.NEEDS_REVIEW}),
    Status.PARSING: frozenset({Status.NORMALIZING, *FAILURE_STATES}),
    Status.NORMALIZING: frozenset({Status.ENRICHING, *FAILURE_STATES}),
    Status.ENRICHING: frozenset({Status.READY_FOR_CHUNKING, *FAILURE_STATES}),
    Status.READY_FOR_CHUNKING: frozenset({Status.CANCELLED}),
    Status.FAILED: frozenset({Status.CANCELLED}),
    Status.QUARANTINED: frozenset({Status.CANCELLED}),
    Status.NEEDS_REVIEW: frozenset({Status.CANCELLED}),
}


# A reparse is deliberately explicit: a completed or flagged job never re-enters the parse path
# on its own, and doing so consumes the same bounded retry budget as a failure retry.
REPARSE_ORIGINS = frozenset({Status.READY_FOR_CHUNKING, Status.NEEDS_REVIEW, Status.FAILED})


def require_transition(
    current: Status, target: Status, *, retry: bool = False, reparse: bool = False
) -> None:
    if reparse:
        if current in REPARSE_ORIGINS and target == Status.VALIDATING:
            return
        raise DomainError(
            "INGESTION_INVALID_TRANSITION",
            "Only parsed, flagged or failed jobs can be reparsed.",
            409,
        )
    if retry:
        if current == Status.FAILED and target == Status.VALIDATING:
            return
        raise DomainError("INGESTION_INVALID_TRANSITION", "Only failed jobs can be retried.", 409)
    if target not in TRANSITIONS.get(current, frozenset()):
        raise DomainError(
            "INGESTION_INVALID_TRANSITION",
            f"Cannot move an ingestion job from {current} to {target}.",
            409,
        )


def record_initial(session: Session, job: IngestionJob, actor: UUID) -> None:
    _event(session, job, None, actor, "api")


def _event(
    session: Session, job: IngestionJob, previous: Status | None, actor: UUID | None, service: str
) -> None:
    session.add(
        IngestionStageEvent(
            ingestion_job_id=job.id,
            sequence=int(
                session.scalar(
                    select(func.coalesce(func.max(IngestionStageEvent.sequence), 0)).where(
                        IngestionStageEvent.ingestion_job_id == job.id
                    )
                )
                or 0
            )
            + 1,
            stage=job.current_stage,
            from_status=previous.value if previous else None,
            to_status=job.status.value,
            service_identity=service,
            retry_number=job.retry_count,
            error_code=job.last_error_code,
            error_detail=job.last_error_message,
            correlation_id=job.correlation_id,
        )
    )
    audit(
        session,
        job.tenant_id,
        actor,
        "INGESTION_STATE_CHANGED",
        job.id,
        job.correlation_id,
        {
            "from": previous.value if previous else None,
            "to": job.status.value,
            "retry": job.retry_count,
        },
    )


def transition(
    session: Session,
    job: IngestionJob,
    version: DocumentVersion,
    target: Status,
    actor: UUID | None,
    *,
    service: str = "api",
    retry: bool = False,
    reparse: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    require_transition(job.status, target, retry=retry, reparse=reparse)
    if retry or reparse:
        if job.retry_count >= job.max_retries:
            raise DomainError("INGESTION_RETRY_EXHAUSTED", "The retry limit has been reached.", 409)
        job.retry_count += 1
        job.completed_at = None
        job.queue_received_at = None
    previous = job.status
    job.status = target
    job.current_stage = target.value
    version.ingestion_status = target
    version.searchable = False
    job.last_error_code, job.last_error_message = error_code, error_message
    now = datetime.now(UTC)
    if target == Status.VALIDATING:
        job.started_at = now
    if target == Status.QUEUED:
        job.queued_at = now
    # READY_FOR_CHUNKING completes the M2 job; it is not readiness for retrieval or answering.
    if target in FAILURE_STATES or target == Status.READY_FOR_CHUNKING:
        job.completed_at = now
    if target == Status.CANCELLED:
        job.cancelled_at = now
    _event(session, job, previous, actor, service)
    session.flush()  # Persist each edge, not just the last state in a chained transition.
