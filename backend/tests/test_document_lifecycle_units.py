"""The document lifecycle read model.

What is pinned here is the contract the processing screen depends on:

* a stage's state comes from the job's status and nothing else — never from elapsed time;
* reprocessing starts a new pass without pretending the earlier stages ran again;
* no field anywhere expresses progress as a percentage;
* an estimate exists only when enough comparable completed work exists, and is a range;
* findings are grouped by code *and* severity, blocking first, and repeated warnings collapse;
* a reprocessing action is offered only where the state machine would accept it.

The event sequences are real ones, taken from documents processed on this deployment.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.schemas.lifecycle import Lifecycle, LifecycleStage, LifecycleSummary
from app.security.auth import ROLE_PERMISSIONS
from app.services.lifecycle import (
    MIN_ESTIMATE_SAMPLES,
    STAGES,
    Event,
    actions,
    derive,
    estimate_range,
    group_findings,
    overall_state,
    stage_states,
)

T0 = datetime(2026, 10, 4, 12, 16, 54, tzinfo=UTC)
CODES = [code for code, _ in STAGES]


def events(*steps: tuple[str, float]) -> list[Event]:
    """(status, seconds after T0) pairs, in order."""
    return [
        Event(sequence=index, to_status=status, at=T0 + timedelta(seconds=offset))
        for index, (status, offset) in enumerate(steps, start=1)
    ]


UPLOAD = (("UPLOADED", 0), ("VALIDATING", 0.01), ("QUEUED", 0.02))
READ = (("PARSING", 6), ("NORMALIZING", 305), ("ENRICHING", 306), ("READY_FOR_CHUNKING", 306.1))
PASSAGES = (("CHUNKING", 309), ("VALIDATING_CHUNKS", 309.1))
REST = (
    ("READY_FOR_EMBEDDING", 310),
    ("EMBEDDING", 312),
    ("INDEXING", 360),
    ("VERIFYING_INDEX", 370),
    ("READY_FOR_RETRIEVAL", 371),
    ("SPARSE_INDEXING", 372),
    ("VERIFYING_SPARSE_INDEX", 373),
    ("RETRIEVAL_READY", 374),
)


def states_for(status: str, sequence: list[Event]) -> dict[str, str]:
    return dict(zip(CODES, stage_states(status, derive(sequence)), strict=True))


# ----------------------------------------------------------------- stages come from the status


def test_a_parse_in_progress_has_reading_running_and_nothing_after_it_started():
    sequence = events(*UPLOAD, ("PARSING", 6))
    states = states_for("PARSING", sequence)
    assert states["RECEIVED"] == "COMPLETED"
    assert states["READING"] == "RUNNING"
    assert all(states[code] == "PENDING" for code in CODES[2:])
    assert derive(sequence).run_started_at == T0


def test_a_queued_document_is_in_the_reading_stage_waiting_for_a_worker():
    states = states_for("QUEUED", events(*UPLOAD))
    assert states["READING"] == "RUNNING"


def test_a_retrieval_ready_document_has_every_stage_completed_with_real_durations():
    sequence = events(*UPLOAD, *READ, *PASSAGES, *REST)
    derived = derive(sequence)
    assert set(stage_states("RETRIEVAL_READY", derived)) == {"COMPLETED"}
    reading = CODES.index("READING")
    # Reading began when the job was queued and ended when chunking was first ready.
    assert derived.entered[reading] == T0 + timedelta(seconds=0.02)
    assert derived.left[reading] == T0 + timedelta(seconds=306.1)


def test_the_status_alone_decides_the_running_stage_however_long_it_has_run():
    """A day-long parse is still RUNNING; time never advances a stage."""
    sequence = events(*UPLOAD, ("PARSING", 6))
    assert states_for("PARSING", sequence)["READING"] == "RUNNING"
    stale = [Event(e.sequence, e.to_status, e.at - timedelta(days=1)) for e in sequence]
    assert states_for("PARSING", stale)["READING"] == "RUNNING"


# ---------------------------------------------------------------------- stops and new passes


def real_review_with_one_rechunk() -> list[Event]:
    """Head and Neck: Muscle Charts, as it was processed: review, rechunk, review again."""
    return events(
        *UPLOAD,
        *READ,
        *PASSAGES,
        ("NEEDS_REVIEW", 310),
        ("READY_FOR_CHUNKING", 2236),
        ("CHUNKING", 2239),
        ("VALIDATING_CHUNKS", 2240),
        ("NEEDS_REVIEW", 2241),
    )


def test_review_blocks_the_stage_that_stopped_and_leaves_later_ones_pending():
    states = states_for("NEEDS_REVIEW", real_review_with_one_rechunk())
    assert states["RECEIVED"] == states["READING"] == "COMPLETED"
    assert states["PASSAGES"] == "BLOCKED"
    assert all(states[code] == "PENDING" for code in CODES[3:])


def test_a_rechunk_starts_a_new_pass_without_pretending_the_document_was_read_again():
    derived = derive(real_review_with_one_rechunk())
    # The current pass began with the rechunk, not with the upload.
    assert derived.run_started_at == T0 + timedelta(seconds=2236)
    assert derived.stop_time == T0 + timedelta(seconds=2241)
    # Reading keeps its timing from the first pass: it was not repeated.
    reading = CODES.index("READING")
    assert derived.entered[reading] == T0 + timedelta(seconds=0.02)
    # Passages are timed from the second attempt only.
    passages = CODES.index("PASSAGES")
    assert derived.entered[passages] == T0 + timedelta(seconds=2236)


def test_a_failed_parse_marks_reading_failed():
    states = states_for("FAILED", events(*UPLOAD, ("PARSING", 6), ("FAILED", 9)))
    assert states["READING"] == "FAILED"
    assert states["PASSAGES"] == "PENDING"


def test_cancelling_a_ready_document_does_not_unmark_its_completed_stages():
    """Archiving cancels the job of a ready document; every stage still did complete."""
    sequence = events(*UPLOAD, *READ, *PASSAGES, *REST, ("CANCELLED", 500))
    assert set(stage_states("CANCELLED", derive(sequence))) == {"COMPLETED"}


def test_a_reprocess_sent_back_without_a_stop_resets_only_the_later_stages():
    sequence = events(
        *UPLOAD, *READ, *PASSAGES, *REST, ("READY_FOR_EMBEDDING", 900), ("EMBEDDING", 901)
    )
    derived = derive(sequence)
    states = dict(zip(CODES, stage_states("EMBEDDING", derived), strict=True))
    assert states["PASSAGES"] == "COMPLETED"
    assert states["EMBEDDING"] == "RUNNING"
    assert states["KEYWORD_INDEX"] == states["READY"] == "PENDING"
    assert derived.run_started_at == T0 + timedelta(seconds=900)
    assert CODES.index("READY") not in derived.entered


# ------------------------------------------------------------------------------- overall state


@pytest.mark.parametrize(
    ("status", "archived", "deletion", "expected"),
    [
        ("PARSING", False, None, "PROCESSING"),
        ("QUEUED", False, None, "PROCESSING"),
        ("RETRIEVAL_READY", False, None, "READY"),
        ("NEEDS_REVIEW", False, None, "REVIEW_REQUIRED"),
        ("FAILED", False, None, "FAILED"),
        ("QUARANTINED", False, None, "FAILED"),
        ("CANCELLED", False, None, "CANCELLED"),
        ("CANCELLED", True, None, "ARCHIVED"),
        ("RETRIEVAL_READY", True, "REQUESTED", "DELETING"),
        ("CANCELLED", True, "FAILED", "DELETION_INCOMPLETE"),
        (None, False, None, "NOT_STARTED"),
    ],
)
def test_overall_state(status, archived, deletion, expected):
    assert overall_state(status, archived, deletion) == expected


def test_review_is_never_reported_as_a_technical_failure():
    assert overall_state("NEEDS_REVIEW", False, None) != "FAILED"


# ---------------------------------------------------------------------------- no percentages


@pytest.mark.parametrize("model", [Lifecycle, LifecycleStage, LifecycleSummary])
def test_no_field_expresses_progress_as_a_fraction(model):
    # Whole name parts, so `duration_ms` is not mistaken for a ratio.
    parts = {part for name in model.model_fields for part in name.lower().split("_")}
    assert not parts & {"percent", "percentage", "progress", "fraction", "ratio", "pct"}


# ---------------------------------------------------------------------------------- estimates


def test_no_estimate_without_enough_comparable_runs():
    rates = [4.0] * (MIN_ESTIMATE_SAMPLES - 1)
    assert estimate_range(rates, units=14, elapsed_seconds=0) is None


def test_an_estimate_is_a_range_from_the_middle_of_the_history():
    rates = [3.0, 4.0, 4.0, 5.0, 5.0, 6.0, 20.0, 21.0]
    low, high = estimate_range(rates, units=10, elapsed_seconds=0)
    assert 0 <= low <= high
    # One slow outlier must not set the figure: the range sits inside, not at, the extremes.
    assert high < 21.0 * 10


def test_the_estimate_counts_down_and_never_goes_negative():
    rates = [4.0] * MIN_ESTIMATE_SAMPLES
    full = estimate_range(rates, units=10, elapsed_seconds=0)
    later = estimate_range(rates, units=10, elapsed_seconds=20)
    assert later is not None and full is not None and later[1] < full[1]
    assert later[0] >= 0


def test_a_run_that_outlasts_its_history_gets_no_invented_figure():
    rates = [4.0] * MIN_ESTIMATE_SAMPLES
    assert estimate_range(rates, units=10, elapsed_seconds=10_000) is None


# ----------------------------------------------------------------------------------- findings


def finding(code, severity, page=None):
    return (code, severity, f"{code} message", page, uuid4())


def test_repeated_warnings_collapse_into_one_group_and_the_blocker_comes_first():
    rows = [finding("CHUNK_FIGURE_NO_TEXT", "WARNING", page) for page in range(1, 14)]
    rows.append(finding("CHUNK_OVERSIZED", "ERROR", 10))
    groups = group_findings(rows)
    assert [(group.code, group.count) for group in groups] == [
        ("CHUNK_OVERSIZED", 1),
        ("CHUNK_FIGURE_NO_TEXT", 13),
    ]
    assert groups[0].blocking is True and groups[1].blocking is False
    assert len(groups[1].samples) == 5


def test_one_code_at_two_severities_stays_two_groups():
    """CHUNK_OVERSIZED blocks as an ERROR and does not as a WARNING; merging them would mislabel."""
    groups = group_findings(
        [finding("CHUNK_OVERSIZED", "ERROR"), finding("CHUNK_OVERSIZED", "WARNING")]
    )
    assert [(group.severity, group.blocking) for group in groups] == [
        ("ERROR", True),
        ("WARNING", False),
    ]


def test_critical_blocks_and_info_does_not():
    groups = {
        g.severity: g.blocking
        for g in group_findings([finding("A", "CRITICAL"), finding("B", "INFO")])
    }
    assert groups == {"CRITICAL": True, "INFO": False}


# ------------------------------------------------------------------------------------ actions


def job(status, code=None, retries=0, maximum=3):
    return SimpleNamespace(
        status=status, last_error_code=code, retry_count=retries, max_retries=maximum
    )


def available(item) -> dict[str, object]:
    return {entry.action: entry.available or entry.reason for entry in actions(item)}


def test_a_chunk_review_never_offers_to_embed_an_invalid_chunk_dataset():
    offered = available(job("NEEDS_REVIEW", "CHUNK_NEEDS_REVIEW"))
    assert offered["rechunk"] is True and offered["reparse"] is True
    assert offered["reembed"] == "NOT_APPLICABLE"
    assert offered["reindex-sparse"] == "NOT_APPLICABLE"


def test_a_parse_review_offers_neither_rechunking_nor_embedding():
    offered = available(job("NEEDS_REVIEW", "PARSE_NEEDS_REVIEW"))
    assert offered["rechunk"] == offered["reembed"] == "NOT_APPLICABLE"
    assert offered["reparse"] is True


def test_an_exhausted_retry_budget_is_said_plainly():
    offered = available(job("NEEDS_REVIEW", "CHUNK_NEEDS_REVIEW", retries=3))
    assert offered["rechunk"] == offered["reparse"] == "NO_RETRIES_LEFT"
    # Cancelling spends no retry, so it stays available.
    assert offered["cancel"] is True


def test_retry_is_only_for_a_failure():
    assert available(job("RETRIEVAL_READY"))["retry"] == "NOT_APPLICABLE"
    assert available(job("FAILED", "PARSE_TIMEOUT"))["retry"] is True


# --------------------------------------------------------------------------------- permission


def test_only_an_administrator_may_permanently_delete():
    assert "document:delete" in ROLE_PERMISSIONS["admin"]
    assert "document:delete" not in ROLE_PERMISSIONS["curator"]
    assert "document:delete" not in ROLE_PERMISSIONS["reader"]
    # Archiving stays a curator capability; the two are deliberately separate authorities.
    assert "document:manage" in ROLE_PERMISSIONS["curator"]
