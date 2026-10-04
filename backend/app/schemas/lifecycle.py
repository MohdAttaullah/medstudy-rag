"""The document lifecycle as a reader sees it: what has happened, what is happening, what is next.

Every value here is read from persisted state — the job, its stage events and the runs each stage
produced. Nothing is projected from elapsed time and nothing is expressed as a percentage, because
the stages differ in length by three orders of magnitude: reading a scanned book takes an hour,
building its keyword index takes a second, so "four of seven stages" says nothing about how much
work is left.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

LifecycleState = Literal[
    "PROCESSING",
    "REVIEW_REQUIRED",
    "FAILED",
    "CANCELLED",
    "READY",
    "ARCHIVED",
    "NOT_STARTED",
]
StageCode = Literal[
    "RECEIVED", "READING", "PASSAGES", "EMBEDDING", "SEMANTIC_INDEX", "KEYWORD_INDEX", "READY"
]
StageState = Literal["COMPLETED", "RUNNING", "PENDING", "BLOCKED", "FAILED", "CANCELLED"]
Severity = Literal["CRITICAL", "ERROR", "WARNING", "INFO"]
ActionName = Literal["retry", "reparse", "rechunk", "reembed", "reindex-sparse", "cancel"]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LifecycleStage(Frozen):
    code: StageCode
    state: StageState
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = None
    # Counts the stage actually produced (pages read, passages made, vectors verified). Present only
    # for a stage of the current pass that has started; a pending stage reports nothing, because a
    # number left over from an earlier attempt would describe a dataset that no longer exists.
    facts: dict[str, int | str | None] = {}


class Estimate(Frozen):
    """Time left in the current stage, or the reason there is no honest figure."""

    available: bool
    stage: StageCode | None = None
    low_seconds: int | None = None
    high_seconds: int | None = None
    samples: int = 0
    needed: int = 0
    reason: (
        Literal[
            "INSUFFICIENT_HISTORY", "LONGER_THAN_USUAL", "NOT_ESTIMATED_FOR_STAGE", "NOT_RUNNING"
        ]
        | None
    ) = None


class FindingSample(Frozen):
    page: int | None = None
    chunk_id: UUID | None = None


class FindingGroup(Frozen):
    code: str
    severity: Severity
    # Taken from the validator's own rule: CRITICAL fails a run, ERROR stops it for review,
    # WARNING and INFO never stop anything.
    blocking: bool
    count: int
    message: str
    samples: list[FindingSample] = []


class Review(Frozen):
    stage: Literal["PARSE", "CHUNK", "OTHER"]
    run_id: UUID | None = None
    blocking_count: int
    warning_count: int
    info_count: int
    groups: list[FindingGroup]
    # Earlier attempts at this stage that used the same code and settings and stopped on the same
    # blocking findings. When this is non-zero, repeating the attempt unchanged will not help.
    identical_earlier_attempts: int = 0
    retries_left: int
    max_retries: int


class ActionAvailability(Frozen):
    action: ActionName
    available: bool
    reason: Literal["NO_RETRIES_LEFT", "NOT_APPLICABLE"] | None = None


class Failure(Frozen):
    code: str | None
    message: str | None


class Lifecycle(Frozen):
    document_id: UUID
    version_id: UUID | None
    version_number: int | None
    job_id: UUID | None
    state: LifecycleState
    status: str | None
    current_stage: StageCode | None
    # The raw status inside the current stage, so a reader can be told "waiting for a worker"
    # rather than "reading" while a parse is still queued.
    activity: str | None
    terminal: bool
    run_started_at: datetime | None
    finished_at: datetime | None
    current_stage_started_at: datetime | None
    # The parse worker's last heartbeat: proof the machine is alive during a long read, which is
    # the question a static page cannot answer.
    heartbeat_at: datetime | None
    server_time: datetime
    stages: list[LifecycleStage]
    estimate: Estimate
    review: Review | None = None
    failure: Failure | None = None
    actions: list[ActionAvailability] = []


class LifecycleSummary(Frozen):
    """The compact form the Library shows in one table cell."""

    state: LifecycleState
    current_stage: StageCode | None
    activity: str | None
    run_started_at: datetime | None
    finished_at: datetime | None
    server_time: datetime
    blocking_count: int = 0
    warning_count: int = 0
