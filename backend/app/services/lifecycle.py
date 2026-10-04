"""The document lifecycle read model.

The ingestion pipeline has seventeen forward states and four ways to stop. A reader does not need
seventeen states; they need to know whether the document is ready, what is happening now, and what
— if anything — they must do. This module groups the states into seven stages a person recognises
and reconstructs each stage's timing from the job's own append-only stage events, so the picture
survives a page reload and is identical for every viewer.

Three rules hold throughout:

* The backend is the only source of progress. A stage is RUNNING because the job's status is in
  it, COMPLETED because a later stage was entered, and nothing else moves it.
* There are no percentages. Stages differ in length by orders of magnitude; counting them would
  mislead. Where a stage has a real unit count (pages, passages, vectors), that count is reported.
* An estimate is offered only when enough comparable completed work exists on this deployment,
  and then only as a range for the current stage. Otherwise the response says why there is none.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import quantiles
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.chunking_config import ChunkingConfig
from app.ingestion.state import (
    RECHUNK_ORIGINS,
    REEMBED_ORIGINS,
    REINDEX_SPARSE_ORIGINS,
    REPARSE_ORIGINS,
)
from app.models.chunking import Chunk, ChunkRun, ChunkValidationFinding
from app.models.documents import (
    Document,
    DocumentDeletion,
    DocumentVersion,
    IngestionJob,
    IngestionStageEvent,
)
from app.models.embeddings import EmbeddingRun, IndexRun
from app.models.enums import Status
from app.models.parsing import ParseRun, ParseValidationFinding
from app.models.retrieval import SparseIndex
from app.schemas.lifecycle import (
    ActionAvailability,
    Estimate,
    Failure,
    FindingGroup,
    FindingSample,
    Lifecycle,
    LifecycleStage,
    LifecycleSummary,
    Review,
)

# --------------------------------------------------------------------------------------- stages

#: Stage code and the pipeline statuses it covers, in pipeline order. Waiting states belong to the
#: stage they wait for (QUEUED is the start of reading; READY_FOR_EMBEDDING the start of embedding),
#: so a stage that is "running" may honestly be waiting for a worker — `activity` says which.
STAGES: tuple[tuple[str, tuple[Status, ...]], ...] = (
    ("RECEIVED", (Status.UPLOADED, Status.VALIDATING)),
    ("READING", (Status.QUEUED, Status.PARSING, Status.NORMALIZING, Status.ENRICHING)),
    ("PASSAGES", (Status.READY_FOR_CHUNKING, Status.CHUNKING, Status.VALIDATING_CHUNKS)),
    ("EMBEDDING", (Status.READY_FOR_EMBEDDING, Status.EMBEDDING)),
    ("SEMANTIC_INDEX", (Status.INDEXING, Status.VERIFYING_INDEX)),
    (
        "KEYWORD_INDEX",
        (Status.READY_FOR_RETRIEVAL, Status.SPARSE_INDEXING, Status.VERIFYING_SPARSE_INDEX),
    ),
    ("READY", (Status.RETRIEVAL_READY,)),
)
STAGE_OF: dict[str, int] = {
    status.value: index for index, (_, statuses) in enumerate(STAGES) for status in statuses
}
STOPS = frozenset({Status.FAILED, Status.QUARANTINED, Status.NEEDS_REVIEW, Status.CANCELLED})
READY_INDEX = len(STAGES) - 1

#: How many comparable completed runs an estimate needs. Below this, a range would be a guess
#: dressed as a measurement; the response says how many exist instead.
MIN_ESTIMATE_SAMPLES = 8
BLOCKING = frozenset({"CRITICAL", "ERROR"})
#: The chunker this code installs. A Literal in the policy, so configuration cannot change it.
CURRENT_CHUNKER: str = ChunkingConfig.model_fields["chunker_version"].default


@dataclass(frozen=True)
class Event:
    sequence: int
    to_status: str
    at: datetime


@dataclass
class Derived:
    """What the stage events say, independent of how they are presented."""

    entered: dict[int, datetime] = field(default_factory=dict)
    left: dict[int, datetime] = field(default_factory=dict)
    stop_stage: int | None = None
    stop_time: datetime | None = None
    run_started_at: datetime | None = None


def derive(events: Sequence[Event]) -> Derived:
    """Reconstruct, for the *current* pass, when each stage was entered and left.

    A pass ends at a stop (review, failure, cancellation). Reprocessing then sends the job back to
    an earlier stage; from that event on, stages at or after it belong to a new pass, so their old
    timings are discarded while the earlier stages keep theirs — a rechunk does not re-read the
    document, and the page should not pretend it did.
    """
    result = Derived()
    current: int | None = None
    for event in sorted(events, key=lambda item: item.sequence):
        stage = STAGE_OF.get(event.to_status)
        if stage is None:
            # A stop. The stage the job was in ends here.
            if current is not None and current not in result.left:
                result.left[current] = event.at
            result.stop_stage, result.stop_time = current, event.at
            continue
        # A pipeline event after a stop begins a new pass; so does being sent back to an earlier
        # stage without one (a reprocess requested from a completed state).
        restarted = result.stop_time is not None
        sent_back = current is not None and stage < current
        if result.run_started_at is None or restarted or sent_back:
            result.run_started_at = event.at
        if restarted or sent_back:
            for later in [index for index in result.entered if index >= stage]:
                result.entered.pop(later, None)
                result.left.pop(later, None)
            result.stop_stage = result.stop_time = None
        if current is not None and stage != current and current not in result.left:
            result.left[current] = event.at
        result.entered.setdefault(stage, event.at)
        current = stage
    return result


def stage_states(status: str, derived: Derived) -> list[str]:
    """One state per stage, read from the job's status — never from the clock."""
    if status == Status.RETRIEVAL_READY:
        return ["COMPLETED"] * len(STAGES)
    if status in STOPS:
        stop = derived.stop_stage if derived.stop_stage is not None else 0
        marker = {
            Status.NEEDS_REVIEW: "BLOCKED",
            Status.FAILED: "FAILED",
            Status.QUARANTINED: "FAILED",
            Status.CANCELLED: "CANCELLED",
        }[Status(status)]
        if stop == READY_INDEX:
            # Cancelled after it was ready (archiving does this): every stage did complete.
            return ["COMPLETED"] * len(STAGES)
        return [
            "COMPLETED" if index < stop else marker if index == stop else "PENDING"
            for index in range(len(STAGES))
        ]
    current = STAGE_OF.get(status, 0)
    return [
        "COMPLETED" if index < current else "RUNNING" if index == current else "PENDING"
        for index in range(len(STAGES))
    ]


def overall_state(status: str | None, archived: bool, deletion_status: str | None) -> str:
    if deletion_status == "REQUESTED":
        return "DELETING"
    if deletion_status == "FAILED":
        return "DELETION_INCOMPLETE"
    if archived:
        return "ARCHIVED"
    if status is None:
        return "NOT_STARTED"
    if status == Status.RETRIEVAL_READY:
        return "READY"
    if status == Status.NEEDS_REVIEW:
        return "REVIEW_REQUIRED"
    if status in {Status.FAILED, Status.QUARANTINED}:
        return "FAILED"
    if status == Status.CANCELLED:
        return "CANCELLED"
    return "PROCESSING"


# ------------------------------------------------------------------------------------ estimate


def estimate_range(
    rates: Sequence[float], units: int, elapsed_seconds: float
) -> tuple[int, int] | None:
    """Remaining seconds as the interquartile range of comparable runs, or None.

    `rates` are seconds per unit (page, passage) from completed runs. The middle half is used
    rather than a mean so one pathological document cannot drag the figure. A result is only
    returned when the slower end of that range is still ahead; once the run has outlasted it, any
    number would be invented, and the caller says the run is taking longer than usual instead.
    """
    if len(rates) < MIN_ESTIMATE_SAMPLES or units <= 0:
        return None
    low_rate, _, high_rate = quantiles(sorted(rates), n=4)
    high = high_rate * units - elapsed_seconds
    if high <= 0:
        return None
    low = max(0.0, low_rate * units - elapsed_seconds)
    return int(low), int(max(high, low))


def _parse_rates(session: Session) -> list[float]:
    # Platform-wide on purpose: these are machine throughput figures — seconds per page — with no
    # tenant, document or content attached, and a tenant's own history is usually too thin to
    # estimate from. Single-page fixtures are excluded; their fixed start-up cost dominates.
    rows = session.execute(
        select(ParseRun.duration_ms, ParseRun.page_count).where(
            ParseRun.status.in_(("SUCCEEDED", "REVIEWED_ACCEPTED")),
            ParseRun.page_count >= 5,
            ParseRun.duration_ms > 0,
        )
    ).all()
    return [duration / 1000 / pages for duration, pages in rows if pages]


def _embedding_rates(session: Session) -> list[float]:
    rows = session.execute(
        select(EmbeddingRun.duration_ms, EmbeddingRun.embedded_chunk_count).where(
            EmbeddingRun.status == "SUCCEEDED",
            EmbeddingRun.embedded_chunk_count >= 20,
            EmbeddingRun.duration_ms > 0,
        )
    ).all()
    return [duration / 1000 / count for duration, count in rows if count]


def _estimate(
    session: Session,
    status: str,
    stage_entered: datetime | None,
    pages: int | None,
    passages: int | None,
    now: datetime,
) -> Estimate:
    if status not in {Status.PARSING, Status.EMBEDDING} or stage_entered is None:
        reason: Literal["NOT_RUNNING", "NOT_ESTIMATED_FOR_STAGE"] = (
            "NOT_RUNNING"
            if status in STOPS or status == Status.RETRIEVAL_READY
            else "NOT_ESTIMATED_FOR_STAGE"
        )
        return Estimate(available=False, reason=reason, needed=MIN_ESTIMATE_SAMPLES)
    if status == Status.PARSING:
        stage, rates, units = "READING", _parse_rates(session), pages or 0
    else:
        stage, rates, units = "EMBEDDING", _embedding_rates(session), passages or 0
    elapsed = max(0.0, (now - stage_entered).total_seconds())
    if len(rates) < MIN_ESTIMATE_SAMPLES or units <= 0:
        return Estimate(
            available=False,
            stage=stage,  # type: ignore[arg-type]
            samples=len(rates),
            needed=MIN_ESTIMATE_SAMPLES,
            reason="INSUFFICIENT_HISTORY",
        )
    window = estimate_range(rates, units, elapsed)
    if window is None:
        return Estimate(
            available=False,
            stage=stage,  # type: ignore[arg-type]
            samples=len(rates),
            needed=MIN_ESTIMATE_SAMPLES,
            reason="LONGER_THAN_USUAL",
        )
    return Estimate(
        available=True,
        stage=stage,  # type: ignore[arg-type]
        low_seconds=window[0],
        high_seconds=window[1],
        samples=len(rates),
        needed=MIN_ESTIMATE_SAMPLES,
    )


# --------------------------------------------------------------------------------------- review


def group_findings(
    rows: Iterable[tuple[str, str, str, int | None, UUID | None]],
) -> list[FindingGroup]:
    """Collapse findings to one group per (code, severity), blocking groups first.

    Keyed on severity as well as code because the validator uses one code at two severities —
    `CHUNK_OVERSIZED` is a blocking ERROR when a unit cannot be embedded and a WARNING when an
    atomic unit merely exceeds its target. Treating them as one would mislabel one of them.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for code, severity, message, page, chunk_id in rows:
        key = (code, severity)
        group = groups.setdefault(
            key, {"code": code, "severity": severity, "message": message, "count": 0, "samples": []}
        )
        group["count"] += 1
        if len(group["samples"]) < 5:
            group["samples"].append(FindingSample(page=page, chunk_id=chunk_id))
    order = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}
    return [
        FindingGroup(blocking=item["severity"] in BLOCKING, **item)
        for item in sorted(
            groups.values(), key=lambda item: (order.get(item["severity"], 9), item["code"])
        )
    ]


def _review(session: Session, job: IngestionJob, stop_stage: int | None) -> Review:
    code = job.last_error_code or ""
    stage = (
        "PARSE"
        if code.startswith("PARSE") or stop_stage == STAGE_OF[Status.PARSING]
        else "CHUNK"
        if code.startswith("CHUNK") or stop_stage == STAGE_OF[Status.CHUNKING]
        else "OTHER"
    )
    groups: list[FindingGroup] = []
    run_id: UUID | None = None
    reviewed_chunker: str | None = None
    identical = 0
    if stage == "PARSE":
        run = session.scalar(
            select(ParseRun)
            .where(ParseRun.document_version_id == job.document_version_id)
            .order_by(ParseRun.created_at.desc())
            .limit(1)
        )
        if run is not None:
            run_id = run.id
            parse_rows = session.execute(
                select(
                    ParseValidationFinding.code,
                    ParseValidationFinding.severity,
                    ParseValidationFinding.message,
                    ParseValidationFinding.page_number,
                )
                .where(ParseValidationFinding.parse_run_id == run.id)
                .order_by(ParseValidationFinding.page_number, ParseValidationFinding.id)
            ).all()
            groups = group_findings(
                (code, _text(severity), message, page, None)
                for code, severity, message, page in parse_rows
            )
    elif stage == "CHUNK":
        runs = list(
            session.scalars(
                select(ChunkRun)
                .where(ChunkRun.document_version_id == job.document_version_id)
                .order_by(ChunkRun.created_at.desc())
            )
        )
        if runs:
            latest = runs[0]
            run_id = latest.id
            reviewed_chunker = latest.chunker_version
            chunk_rows = session.execute(
                select(
                    ChunkValidationFinding.code,
                    ChunkValidationFinding.severity,
                    ChunkValidationFinding.message,
                    Chunk.page_start,
                    ChunkValidationFinding.chunk_id,
                )
                .outerjoin(
                    Chunk,
                    (Chunk.id == ChunkValidationFinding.chunk_id)
                    & (Chunk.chunk_run_id == ChunkValidationFinding.chunk_run_id),
                )
                .where(ChunkValidationFinding.chunk_run_id == latest.id)
                .order_by(Chunk.page_start, ChunkValidationFinding.id)
            ).all()
            groups = group_findings(
                (code, _text(severity), message, page, chunk_id)
                for code, severity, message, page, chunk_id in chunk_rows
            )
            blocking = _blocking_codes(session, latest.id)
            for earlier in runs[1:]:
                if (
                    earlier.chunker_version == latest.chunker_version
                    and earlier.policy_fingerprint == latest.policy_fingerprint
                    and earlier.validation_result == latest.validation_result
                    and _blocking_codes(session, earlier.id) == blocking
                ):
                    identical += 1
    return Review(
        stage=stage,  # type: ignore[arg-type]
        run_id=run_id,
        blocking_count=sum(group.count for group in groups if group.blocking),
        warning_count=sum(group.count for group in groups if group.severity == "WARNING"),
        info_count=sum(group.count for group in groups if group.severity == "INFO"),
        groups=groups,
        identical_earlier_attempts=identical,
        reviewed_chunker_version=reviewed_chunker,
        current_chunker_version=CURRENT_CHUNKER if stage == "CHUNK" else None,
        retries_left=max(0, job.max_retries - job.retry_count),
        max_retries=job.max_retries,
    )


def _text(value: Any) -> str:
    """An enum member or a plain string, as its string value."""
    return str(getattr(value, "value", value))


def _blocking_codes(session: Session, chunk_run_id: UUID) -> frozenset[str]:
    return frozenset(
        session.scalars(
            select(ChunkValidationFinding.code).where(
                ChunkValidationFinding.chunk_run_id == chunk_run_id,
                ChunkValidationFinding.severity.in_(tuple(BLOCKING)),
            )
        )
    )


# -------------------------------------------------------------------------------------- actions


def actions(job: IngestionJob) -> list[ActionAvailability]:
    """Which reprocessing paths can succeed from here, using the state machine's own origin sets.

    Availability is not authority: the caller still filters by permission, and every endpoint still
    enforces its own transition. The point is to stop offering a button that can only fail — and,
    on a reprocessing path, quietly spend one unit of the bounded retry budget doing so.
    """
    status = Status(job.status)
    code = job.last_error_code or ""
    parse_review = status == Status.NEEDS_REVIEW and code.startswith("PARSE")
    # A chunk dataset that stopped for review was never validated, so nothing downstream of it —
    # embeddings, either index — can be built from it until it is replaced.
    chunk_review = status == Status.NEEDS_REVIEW and code.startswith("CHUNK")
    budget = job.retry_count < job.max_retries

    def entry(name: str, applicable: bool, spends_budget: bool = True) -> ActionAvailability:
        if not applicable:
            return ActionAvailability(action=name, available=False, reason="NOT_APPLICABLE")  # type: ignore[arg-type]
        if spends_budget and not budget:
            return ActionAvailability(action=name, available=False, reason="NO_RETRIES_LEFT")  # type: ignore[arg-type]
        return ActionAvailability(action=name, available=True)  # type: ignore[arg-type]

    return [
        entry("retry", status == Status.FAILED),
        entry("reparse", status in REPARSE_ORIGINS),
        entry("rechunk", status in RECHUNK_ORIGINS and not parse_review),
        entry("reembed", status in REEMBED_ORIGINS and not parse_review and not chunk_review),
        entry(
            "reindex-sparse",
            status in REINDEX_SPARSE_ORIGINS and not parse_review and not chunk_review,
        ),
        entry("cancel", status != Status.CANCELLED, spends_budget=False),
    ]


# ------------------------------------------------------------------------------------ assembling


def _latest(session: Session, model: Any, version_id: UUID) -> Any:
    return session.scalar(
        select(model)
        .where(model.document_version_id == version_id)
        .order_by(model.created_at.desc())
        .limit(1)
    )


def _facts(
    session: Session, version: DocumentVersion, states: list[str]
) -> tuple[list[dict[str, Any]], int | None, int | None, datetime | None]:
    """Counts per stage, only for stages of the current pass that have started."""
    facts: list[dict[str, Any]] = [{} for _ in STAGES]
    started = [state != "PENDING" for state in states]
    pages = version.page_count
    passages: int | None = None
    heartbeat: datetime | None = None
    if started[0]:
        facts[0] = {"pages": version.page_count, "file_size_bytes": version.file_size_bytes}
    parse = _latest(session, ParseRun, version.id)
    if started[1] and parse is not None:
        pages = parse.page_count or pages
        facts[1] = {
            "pages": parse.page_count,
            "ocr_pages": parse.ocr_page_count,
            "elements": parse.element_count,
            "tables": parse.table_count,
            "figures": parse.figure_count,
            "validation": parse.validation_result,
        }
        if parse.status == "RUNNING":
            heartbeat = parse.heartbeat_at
    chunk = _latest(session, ChunkRun, version.id)
    if started[2] and chunk is not None:
        passages = int(
            session.scalar(select(func.count()).where(Chunk.chunk_run_id == chunk.id)) or 0
        )
        severities: dict[str, int] = {
            _text(severity): int(count)
            for severity, count in session.execute(
                select(ChunkValidationFinding.severity, func.count())
                .where(ChunkValidationFinding.chunk_run_id == chunk.id)
                .group_by(ChunkValidationFinding.severity)
            ).all()
        }
        facts[2] = {
            "passages": passages,
            "validation": chunk.validation_result,
            "blocking": int(severities.get("ERROR", 0)) + int(severities.get("CRITICAL", 0)),
            "warnings": int(severities.get("WARNING", 0)),
        }
    embedding = _latest(session, EmbeddingRun, version.id)
    if started[3] and embedding is not None:
        facts[3] = {
            "embedded": embedding.embedded_chunk_count,
            "eligible": embedding.eligible_chunk_count,
        }
    index = _latest(session, IndexRun, version.id)
    if started[4] and index is not None:
        facts[4] = {
            "expected_vectors": index.expected_point_count,
            "verified_vectors": index.verified_point_count,
        }
    sparse = _latest(session, SparseIndex, version.id)
    if started[5] and sparse is not None:
        facts[5] = {
            "expected_passages": sparse.expected_chunk_count,
            "verified_passages": sparse.verified_chunk_count,
            "terms": sparse.term_count,
        }
    return facts, pages, passages, heartbeat


def _events(session: Session, job_id: UUID) -> list[Event]:
    return [
        Event(sequence=row.sequence, to_status=str(row.to_status), at=row.created_at)
        for row in session.scalars(
            select(IngestionStageEvent)
            .where(IngestionStageEvent.ingestion_job_id == job_id)
            .order_by(IngestionStageEvent.sequence)
        )
    ]


def _latest_version(session: Session, document: Document) -> DocumentVersion | None:
    return session.scalar(
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.tenant_id == document.tenant_id,
        )
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
    )


def _job(session: Session, version: DocumentVersion) -> IngestionJob | None:
    return session.scalar(
        select(IngestionJob)
        .where(
            IngestionJob.document_version_id == version.id,
            IngestionJob.tenant_id == version.tenant_id,
        )
        .order_by(IngestionJob.created_at.desc())
        .limit(1)
    )


def deletion_status(session: Session, document: Document) -> str | None:
    return session.scalar(
        select(DocumentDeletion.status).where(
            DocumentDeletion.tenant_id == document.tenant_id,
            DocumentDeletion.document_id == document.id,
        )
    )


def lifecycle(session: Session, document: Document, now: datetime | None = None) -> Lifecycle:
    """The full lifecycle of a document's latest version, as one consistent read."""
    now = now or datetime.now(UTC)
    version = _latest_version(session, document)
    job = _job(session, version) if version is not None else None
    deleting = deletion_status(session, document)
    state = overall_state(
        str(job.status) if job else None, document.archived_at is not None, deleting
    )
    if version is None or job is None:
        return Lifecycle(
            document_id=document.id,
            version_id=version.id if version else None,
            version_number=version.version_number if version else None,
            job_id=None,
            state=state,  # type: ignore[arg-type]
            status=None,
            current_stage=None,
            activity=None,
            terminal=True,
            run_started_at=None,
            finished_at=None,
            current_stage_started_at=None,
            heartbeat_at=None,
            server_time=now,
            stages=[LifecycleStage(code=code, state="PENDING") for code, _ in STAGES],  # type: ignore[arg-type]
            estimate=Estimate(available=False, reason="NOT_RUNNING", needed=MIN_ESTIMATE_SAMPLES),
        )
    status = str(job.status)
    events = _events(session, job.id)
    derived = derive(events)
    states = stage_states(status, derived)
    facts, pages, passages, heartbeat = _facts(session, version, states)
    current = STAGE_OF.get(status)
    stop_or_current = current if current is not None else derived.stop_stage
    stages = []
    for index, (code, _) in enumerate(STAGES):
        started = derived.entered.get(index)
        ended = derived.left.get(index)
        stages.append(
            LifecycleStage(
                code=code,  # type: ignore[arg-type]
                state=states[index],  # type: ignore[arg-type]
                started_at=started if states[index] != "PENDING" else None,
                completed_at=ended if states[index] not in {"PENDING", "RUNNING"} else None,
                duration_ms=(
                    (ended - started).total_seconds() * 1000
                    if started and ended and states[index] not in {"PENDING", "RUNNING"}
                    else None
                ),
                facts=facts[index],
            )
        )
    status_entered = None
    if current is not None:
        # When the job entered its current status — the start of an estimate's clock.
        status_entered = max(
            (event.at for event in events if event.to_status == status), default=None
        )
    terminal = status in STOPS or status == Status.RETRIEVAL_READY
    finished = (
        derived.stop_time
        if status in STOPS
        else derived.entered.get(READY_INDEX)
        if status == Status.RETRIEVAL_READY
        else None
    )
    return Lifecycle(
        document_id=document.id,
        version_id=version.id,
        version_number=version.version_number,
        job_id=job.id,
        state=state,  # type: ignore[arg-type]
        status=status,
        current_stage=STAGES[stop_or_current][0] if stop_or_current is not None else None,  # type: ignore[arg-type]
        activity=status if not terminal else None,
        terminal=terminal or state in {"ARCHIVED", "DELETING", "DELETION_INCOMPLETE"},
        run_started_at=derived.run_started_at,
        finished_at=finished,
        current_stage_started_at=(
            derived.entered.get(stop_or_current) if stop_or_current is not None else None
        ),
        heartbeat_at=heartbeat,
        server_time=now,
        stages=stages,
        estimate=_estimate(session, status, status_entered, pages, passages, now),
        review=_review(session, job, derived.stop_stage) if status == Status.NEEDS_REVIEW else None,
        failure=(
            Failure(code=job.last_error_code, message=job.last_error_message)
            if status in {Status.FAILED, Status.QUARANTINED}
            else None
        ),
        actions=actions(job),
    )


def summary(session: Session, document: Document, now: datetime | None = None) -> LifecycleSummary:
    """The Library's one-cell view. Cheaper than `lifecycle`: no facts, no estimate."""
    now = now or datetime.now(UTC)
    version = _latest_version(session, document)
    job = _job(session, version) if version is not None else None
    state = overall_state(
        str(job.status) if job else None,
        document.archived_at is not None,
        deletion_status(session, document),
    )
    if job is None:
        return LifecycleSummary(
            state=state,  # type: ignore[arg-type]
            current_stage=None,
            activity=None,
            run_started_at=None,
            finished_at=None,
            server_time=now,
        )
    status = str(job.status)
    derived = derive(_events(session, job.id))
    current = STAGE_OF.get(status)
    stage = current if current is not None else derived.stop_stage
    blocking = warnings = 0
    if status == Status.NEEDS_REVIEW:
        review = _review(session, job, derived.stop_stage)
        blocking, warnings = review.blocking_count, review.warning_count
    return LifecycleSummary(
        state=state,  # type: ignore[arg-type]
        current_stage=STAGES[stage][0] if stage is not None else None,  # type: ignore[arg-type]
        activity=status if status not in STOPS and status != Status.RETRIEVAL_READY else None,
        run_started_at=derived.run_started_at,
        finished_at=derived.stop_time if status in STOPS else derived.entered.get(READY_INDEX),
        server_time=now,
        blocking_count=blocking,
        warning_count=warnings,
    )
