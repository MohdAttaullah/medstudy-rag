"""Reviewed parse acceptance: the rules that do not need a database.

The model being protected: a parse the deterministic quality layer flagged keeps its verdict and
its findings forever, and a curator's acceptance is a separate fact. Nothing here may make
`NEEDS_REVIEW` look like `PASS`.
"""

from uuid import uuid4

import pytest
from app.core.errors import DomainError
from app.ingestion.state import ACCEPT_ORIGINS, TRANSITIONS, require_transition
from app.models.enums import ParseResult, ParseRunStatus, Status
from app.repositories.chunking import usable_parse
from app.security.auth import PERMISSIONS, ROLE_PERMISSIONS


class Run:
    """The three fields the chunking predicate reads."""

    def __init__(self, status: str, result: str | None, is_active: bool = True) -> None:
        self.status, self.validation_result, self.is_active = status, result, is_active


# ------------------------------------------------------------------------- the state machine


def test_acceptance_moves_a_flagged_job_into_chunking():
    require_transition(Status.NEEDS_REVIEW, Status.READY_FOR_CHUNKING, accept=True)


def test_the_ordinary_graph_still_offers_a_flagged_job_nothing_but_cancellation():
    """Acceptance is a guarded edge, not a widening of the graph.

    If `NEEDS_REVIEW -> READY_FOR_CHUNKING` were added to TRANSITIONS itself, any caller could
    take it without a decision ever being recorded.
    """
    assert TRANSITIONS[Status.NEEDS_REVIEW] == frozenset({Status.CANCELLED})
    with pytest.raises(DomainError, match="INGESTION_INVALID_TRANSITION"):
        require_transition(Status.NEEDS_REVIEW, Status.READY_FOR_CHUNKING)


@pytest.mark.parametrize(
    "origin",
    [Status.FAILED, Status.READY_FOR_CHUNKING, Status.RETRIEVAL_READY, Status.QUEUED],
)
def test_only_a_flagged_job_can_be_accepted(origin):
    with pytest.raises(DomainError, match="INGESTION_INVALID_TRANSITION"):
        require_transition(origin, Status.READY_FOR_CHUNKING, accept=True)


@pytest.mark.parametrize(
    "target", [Status.READY_FOR_EMBEDDING, Status.RETRIEVAL_READY, Status.CHUNKING, Status.READY]
)
def test_acceptance_cannot_skip_stages(target):
    """Acceptance admits a parse into chunking; it is not a way to jump the pipeline."""
    with pytest.raises(DomainError, match="INGESTION_INVALID_TRANSITION"):
        require_transition(Status.NEEDS_REVIEW, target, accept=True)


def test_acceptance_has_exactly_one_origin():
    assert ACCEPT_ORIGINS == frozenset({Status.NEEDS_REVIEW})


class FakeSession:
    """Enough of a Session for `transition` to record its events."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    def scalar(self, *_: object, **__: object) -> int:
        return 0

    def flush(self) -> None:
        return None


def _job_and_version():
    from app.models.documents import DocumentVersion, IngestionJob

    job = IngestionJob()
    job.id, job.tenant_id = uuid4(), uuid4()
    job.status, job.current_stage = Status.NEEDS_REVIEW, Status.NEEDS_REVIEW.value
    job.retry_count, job.max_retries = 2, 3
    job.correlation_id = uuid4()
    job.last_error_code = job.last_error_message = None
    job.started_at = job.queued_at = job.completed_at = job.cancelled_at = None
    job.queue_received_at = None
    version = DocumentVersion()
    version.ingestion_status, version.searchable = Status.NEEDS_REVIEW, False
    return job, version


def test_acceptance_does_not_consume_a_retry():
    """The retry budget bounds reprocessing loops.

    Acceptance creates no run and reprocesses nothing — it admits the dataset the curator just
    examined — so spending a retry on it would punish review and could exhaust the budget of a
    document that still needs a genuine reparse later.
    """
    from app.ingestion.state import transition

    job, version = _job_and_version()
    transition(FakeSession(), job, version, Status.READY_FOR_CHUNKING, None, accept=True)
    assert job.retry_count == 2, "acceptance must leave the retry budget untouched"
    assert job.status is Status.READY_FOR_CHUNKING
    assert version.ingestion_status is Status.READY_FOR_CHUNKING


def test_a_reparse_by_contrast_still_consumes_one():
    from app.ingestion.state import transition

    job, version = _job_and_version()
    transition(FakeSession(), job, version, Status.VALIDATING, None, reparse=True)
    assert job.retry_count == 3


def test_acceptance_is_refused_once_the_retry_budget_is_spent_only_for_reprocessing():
    """An exhausted budget must not block review; it must block reprocessing."""
    from app.core.errors import DomainError as Failure
    from app.ingestion.state import transition

    job, version = _job_and_version()
    job.retry_count = 3
    transition(FakeSession(), job, version, Status.READY_FOR_CHUNKING, None, accept=True)
    assert job.retry_count == 3

    spent, version = _job_and_version()
    spent.retry_count = 3
    with pytest.raises(Failure, match="INGESTION_RETRY_EXHAUSTED"):
        transition(FakeSession(), spent, version, Status.VALIDATING, None, reparse=True)


# --------------------------------------------------------------------- chunking eligibility


def test_the_automatic_pass_path_is_unchanged():
    assert usable_parse(Run("SUCCEEDED", "PASS"))
    assert usable_parse(Run("SUCCEEDED", "PASS_WITH_WARNINGS"))


def test_a_reviewed_acceptance_is_usable_while_still_flagged():
    """This is the whole point: usable downstream, and still visibly NEEDS_REVIEW."""
    run = Run("REVIEWED_ACCEPTED", "NEEDS_REVIEW")
    assert usable_parse(run)
    assert run.validation_result == "NEEDS_REVIEW"


@pytest.mark.parametrize(
    "run",
    [
        Run("FAILED", "NEEDS_REVIEW"),
        Run("FAILED", "FAIL"),
        Run("SUCCEEDED", "NEEDS_REVIEW"),
        Run("SUCCEEDED", "FAIL"),
        Run("RUNNING", None),
        Run("CANCELLED", None),
        Run("REVIEWED_ACCEPTED", "NEEDS_REVIEW", is_active=False),
        None,
    ],
)
def test_everything_else_stays_ineligible(run):
    assert not usable_parse(run)


def test_an_unreviewed_flagged_parse_is_never_usable():
    """Regression: the state the real 932-page textbook sat in for days."""
    assert not usable_parse(Run("FAILED", "NEEDS_REVIEW", is_active=False))


# ------------------------------------------------------------------------------ permissions


def test_the_accept_capability_exists_and_is_inventoried():
    assert "ingestion:accept" in PERMISSIONS


def test_curators_and_admins_may_accept_and_readers_may_not():
    reader, curator, admin = (ROLE_PERMISSIONS[r] for r in ("reader", "curator", "admin"))
    assert "ingestion:accept" in curator and "ingestion:accept" in admin
    assert "ingestion:accept" not in reader
    assert reader < curator < admin


# ------------------------------------------------------------------------ recorded identity


def test_the_accepted_status_is_distinct_from_automatic_success():
    """`SUCCEEDED` would assert the parse passed, which is false and unrecoverable once written."""
    assert ParseRunStatus.REVIEWED_ACCEPTED.value == "REVIEWED_ACCEPTED"
    assert ParseRunStatus.REVIEWED_ACCEPTED is not ParseRunStatus.SUCCEEDED
    assert ParseResult.NEEDS_REVIEW.value == "NEEDS_REVIEW"


def test_the_decision_request_demands_a_substantive_rationale():
    from app.schemas.review import ReviewDecisionRequest
    from pydantic import ValidationError

    accepted = ReviewDecisionRequest(decision="ACCEPT", rationale="  Reviewed both pages.  ")
    assert accepted.rationale == "Reviewed both pages."
    for rationale in ("", "  ", "ok", "short"):
        with pytest.raises(ValidationError):
            ReviewDecisionRequest(decision="ACCEPT", rationale=rationale)


def test_rejection_is_not_silently_accepted_as_a_decision():
    from app.schemas.review import ReviewDecisionRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewDecisionRequest(decision="REJECT", rationale="Not implemented yet.")
