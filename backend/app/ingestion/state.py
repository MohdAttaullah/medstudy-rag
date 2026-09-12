from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models.documents import DocumentVersion, IngestionJob, IngestionStageEvent
from app.models.enums import Status
from app.observability.ingestion import audit

# M5 stops at RETRIEVAL_READY. The states before it mean progressively more, and the difference
# matters:
#
#   READY_FOR_RETRIEVAL  the dense index for this version verified point for point;
#   RETRIEVAL_READY      the lexical index also verified, and both lanes were built from the same
#                        chunk dataset, so this version can take part in hybrid retrieval.
#
# RETRIEVAL_READY still does **not** mean the document is medically answerable. It says evidence
# candidates can be found, not that any answer may be generated from them: reranking, context
# expansion, evidence sufficiency, grounding and citation validation are later milestones, and
# READY stays unreachable until they exist.
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
    Status.READY_FOR_CHUNKING: frozenset({Status.CHUNKING, *FAILURE_STATES}),
    Status.CHUNKING: frozenset({Status.VALIDATING_CHUNKS, *FAILURE_STATES}),
    Status.VALIDATING_CHUNKS: frozenset({Status.READY_FOR_EMBEDDING, *FAILURE_STATES}),
    Status.READY_FOR_EMBEDDING: frozenset({Status.EMBEDDING, *FAILURE_STATES}),
    Status.EMBEDDING: frozenset({Status.INDEXING, *FAILURE_STATES}),
    Status.INDEXING: frozenset({Status.VERIFYING_INDEX, *FAILURE_STATES}),
    Status.VERIFYING_INDEX: frozenset({Status.READY_FOR_RETRIEVAL, *FAILURE_STATES}),
    Status.READY_FOR_RETRIEVAL: frozenset({Status.SPARSE_INDEXING, *FAILURE_STATES}),
    Status.SPARSE_INDEXING: frozenset({Status.VERIFYING_SPARSE_INDEX, *FAILURE_STATES}),
    Status.VERIFYING_SPARSE_INDEX: frozenset({Status.RETRIEVAL_READY, *FAILURE_STATES}),
    Status.RETRIEVAL_READY: frozenset({Status.CANCELLED}),
    Status.FAILED: frozenset({Status.CANCELLED}),
    Status.QUARANTINED: frozenset({Status.CANCELLED}),
    Status.NEEDS_REVIEW: frozenset({Status.CANCELLED}),
}


# A reparse is deliberately explicit: a completed or flagged job never re-enters the parse path
# on its own, and doing so consumes the same bounded retry budget as a failure retry.
REPARSE_ORIGINS = frozenset(
    {
        Status.READY_FOR_CHUNKING,
        Status.READY_FOR_EMBEDDING,
        Status.READY_FOR_RETRIEVAL,
        Status.RETRIEVAL_READY,
        Status.NEEDS_REVIEW,
        Status.FAILED,
    }
)
RECHUNK_ORIGINS = frozenset(
    {
        Status.READY_FOR_EMBEDDING,
        Status.READY_FOR_RETRIEVAL,
        Status.RETRIEVAL_READY,
        Status.NEEDS_REVIEW,
        Status.FAILED,
    }
)
# Re-embedding rebuilds vectors and the dense index without reparsing or rechunking the source.
# It necessarily rebuilds the lexical index too, because the two lanes must stay on one chunk
# dataset; the pipeline continues through the sparse stages rather than stopping at the dense one.
REEMBED_ORIGINS = frozenset(
    {Status.READY_FOR_RETRIEVAL, Status.RETRIEVAL_READY, Status.NEEDS_REVIEW, Status.FAILED}
)
# Rebuilding only the lexical lane: the analyzer changed, the vectors did not. Deliberately
# explicit, and it consumes the same bounded retry budget as every other reprocessing path.
REINDEX_SPARSE_ORIGINS = frozenset({Status.RETRIEVAL_READY, Status.NEEDS_REVIEW, Status.FAILED})


# Curator acceptance of a parse the quality layer flagged. Unlike every reprocessing path above
# this consumes no retry budget and creates no new run: it admits the *existing* parse dataset,
# which an authorized curator has examined, and is fenced instead by a single immutable decision
# per parse run. It is a separate origin set precisely so it cannot be reached by accident from
# the ordinary graph, which still offers NEEDS_REVIEW nothing but CANCELLED.
ACCEPT_ORIGINS = frozenset({Status.NEEDS_REVIEW})


def require_transition(
    current: Status,
    target: Status,
    *,
    retry: bool = False,
    reparse: bool = False,
    rechunk: bool = False,
    reembed: bool = False,
    resparse: bool = False,
    accept: bool = False,
) -> None:
    if accept:
        if current in ACCEPT_ORIGINS and target == Status.READY_FOR_CHUNKING:
            return
        raise DomainError(
            "INGESTION_INVALID_TRANSITION",
            "Only a parse flagged for review can be accepted into chunking.",
            409,
        )
    if resparse:
        if current in REINDEX_SPARSE_ORIGINS and target == Status.READY_FOR_RETRIEVAL:
            return
        raise DomainError(
            "INGESTION_INVALID_TRANSITION",
            "Only indexed, failed or flagged jobs can have their lexical index rebuilt.",
            409,
        )
    if reembed:
        if current in REEMBED_ORIGINS and target == Status.READY_FOR_EMBEDDING:
            return
        raise DomainError(
            "INGESTION_INVALID_TRANSITION",
            "Only indexed, failed or flagged jobs can be re-embedded.",
            409,
        )
    if rechunk:
        if current in RECHUNK_ORIGINS and target == Status.READY_FOR_CHUNKING:
            return
        raise DomainError(
            "INGESTION_INVALID_TRANSITION",
            "Only completed, failed or flagged chunks can be rebuilt.",
            409,
        )
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
    rechunk: bool = False,
    reembed: bool = False,
    resparse: bool = False,
    accept: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    require_transition(
        job.status,
        target,
        retry=retry,
        reparse=reparse,
        rechunk=rechunk,
        reembed=reembed,
        resparse=resparse,
        accept=accept,
    )
    # `accept` is deliberately absent: admitting a parse a curator has already examined is not
    # reprocessing, so it must not spend a unit of the bounded retry budget that exists to stop
    # reprocessing loops.
    if retry or reparse or rechunk or reembed or resparse:
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
    # READY_FOR_RETRIEVAL is now an intermediate state: the dense index verified and the lexical
    # index is next. RETRIEVAL_READY completes the M5 job. It means both lanes verified on one
    # chunk dataset, not that the document can be answered from: answering is not implemented.
    if target in FAILURE_STATES or target == Status.RETRIEVAL_READY:
        job.completed_at = now
    if target == Status.CANCELLED:
        job.cancelled_at = now
    _event(session, job, previous, actor, service)
    session.flush()  # Persist each edge, not just the last state in a chained transition.
