"""The progress channel in isolation: ordering, thread safety, and what it refuses to carry."""

import asyncio
import re
from pathlib import Path
from uuid import uuid4

import pytest
from app.services.progress import (
    DISCARD,
    STAGES,
    NullReporter,
    QueueReporter,
    StageEvent,
    reporter,
    reporting,
)
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parents[2]


def drain(queue: asyncio.Queue[StageEvent | None]) -> list[StageEvent]:
    events: list[StageEvent] = []
    while not queue.empty():
        event = queue.get_nowait()
        if event is not None:
            events.append(event)
    return events


def test_a_stage_runs_then_completes_with_its_own_elapsed_time():
    async def body() -> None:
        queue: asyncio.Queue[StageEvent | None] = asyncio.Queue()
        progress = QueueReporter(uuid4(), queue)
        progress.start("RETRIEVAL")
        progress.complete("RETRIEVAL")
        events = drain(queue)
        assert [(e.stage, e.state) for e in events] == [
            ("RETRIEVAL", "RUNNING"),
            ("RETRIEVAL", "COMPLETED"),
        ]
        assert events[0].started_at and events[1].completed_at
        assert events[1].elapsed_ms is not None and events[1].elapsed_ms >= 0
        assert [e.sequence for e in events] == [1, 2]

    asyncio.run(body())


def test_a_stage_that_never_started_reports_no_elapsed_time():
    """A skipped stage took no time. Reporting zero would imply it ran and was instant."""

    async def body() -> None:
        queue: asyncio.Queue[StageEvent | None] = asyncio.Queue()
        QueueReporter(uuid4(), queue).skip("GENERATION")
        (event,) = drain(queue)
        assert event.state == "SKIPPED" and event.elapsed_ms is None and event.started_at is None

    asyncio.run(body())


def test_a_stage_already_settled_is_not_reported_twice():
    async def body() -> None:
        queue: asyncio.Queue[StageEvent | None] = asyncio.Queue()
        progress = QueueReporter(uuid4(), queue)
        progress.start("RERANK")
        progress.complete("RERANK")
        progress.complete("RERANK")
        progress.start("RERANK")
        progress.skip("RERANK")
        assert len(drain(queue)) == 2

    asyncio.run(body())


def test_a_stage_still_running_when_the_request_ends_is_reported_failed():
    """Otherwise the interface spins on that stage forever, which is the confusion being fixed."""

    async def body() -> None:
        queue: asyncio.Queue[StageEvent | None] = asyncio.Queue()
        progress = QueueReporter(uuid4(), queue)
        progress.start("GENERATION")
        progress.finish()
        events = drain(queue)
        assert [(e.stage, e.state) for e in events] == [
            ("GENERATION", "RUNNING"),
            ("GENERATION", "FAILED"),
        ]

    asyncio.run(body())


def test_finish_closes_the_stream():
    async def body() -> None:
        queue: asyncio.Queue[StageEvent | None] = asyncio.Queue()
        progress = QueueReporter(uuid4(), queue)
        progress.complete("PREPARING")
        progress.finish()
        seen = []
        while True:
            event = await queue.get()
            if event is None:
                break
            seen.append(event)
        assert [e.stage for e in seen] == ["PREPARING"]

    asyncio.run(body())


def test_a_stage_running_in_a_worker_thread_reports_to_the_same_request():
    """M5 and M6 are synchronous and run through `run_in_threadpool`.

    The context is copied into that thread, so the reporter is the request's own; publishing has to
    hop back to the loop rather than touch the queue from the worker.
    """

    async def body() -> None:
        queue: asyncio.Queue[StageEvent | None] = asyncio.Queue()
        progress = QueueReporter(uuid4(), queue)

        def synchronous_stage() -> None:
            reporter().start("RETRIEVAL")
            reporter().complete("RETRIEVAL")

        with reporting(progress):
            await run_in_threadpool(synchronous_stage)
        # The hop is scheduled on the loop, so give it one turn to land.
        await asyncio.sleep(0)
        events = drain(queue)
        assert [(e.stage, e.state) for e in events] == [
            ("RETRIEVAL", "RUNNING"),
            ("RETRIEVAL", "COMPLETED"),
        ]

    asyncio.run(body())


def test_two_requests_never_see_each_other_stages():
    """Isolation is structural: each request's reporter is bound to its own context."""

    async def body() -> None:
        first: asyncio.Queue[StageEvent | None] = asyncio.Queue()
        second: asyncio.Queue[StageEvent | None] = asyncio.Queue()
        one, two = QueueReporter(uuid4(), first), QueueReporter(uuid4(), second)

        async def ask(progress: QueueReporter, stage: str) -> None:
            with reporting(progress):
                await asyncio.sleep(0)
                reporter().start(stage)  # type: ignore[arg-type]
                reporter().complete(stage)  # type: ignore[arg-type]

        await asyncio.gather(ask(one, "RETRIEVAL"), ask(two, "GENERATION"))
        assert {e.stage for e in drain(first)} == {"RETRIEVAL"}
        assert {e.stage for e in drain(second)} == {"GENERATION"}

    asyncio.run(body())


def test_the_pipeline_reports_to_nothing_when_nobody_is_watching():
    """The JSON endpoint and every direct service call run against this."""
    assert reporter() is DISCARD
    assert isinstance(DISCARD, NullReporter)
    # It accepts every call the protocol defines and does nothing with any of them.
    DISCARD.start("RETRIEVAL")
    DISCARD.complete("RETRIEVAL")
    DISCARD.skip(*STAGES)
    DISCARD.fail("GENERATION")


# ---------------------------------------------------- what the channel refuses to carry


def test_an_event_cannot_be_given_a_field_that_could_hold_content():
    with pytest.raises(ValidationError):
        StageEvent(
            request_id=uuid4(),
            stage="GENERATION",
            state="COMPLETED",
            sequence=1,
            answer="A drafted sentence.",  # type: ignore[call-arg]
        )


def test_the_event_model_has_no_free_text_field_at_all():
    """A future edit cannot add one without this failing, which is the point of the assertion."""
    allowed = {
        "request_id",
        "stage",
        "state",
        "sequence",
        "started_at",
        "completed_at",
        "elapsed_ms",
    }
    assert set(StageEvent.model_fields) == allowed


def test_an_unknown_stage_or_state_is_not_representable():
    for bad in ({"stage": "SUMMARISING"}, {"state": "ALMOST_DONE"}):
        payload = {
            "request_id": str(uuid4()),
            "stage": "GENERATION",
            "state": "RUNNING",
            "sequence": 1,
            **bad,
        }
        with pytest.raises(ValidationError):
            StageEvent.model_validate(payload)


# ---------------------------------------------------- no simulated progress anywhere


def test_no_stage_is_advanced_by_a_timer_or_a_guess():
    """Every state change must come from the code performing the stage.

    A timer that advanced the interface would show a stage the pipeline is not running, which is
    worse than showing nothing: it would look informative and be wrong. A clock counting elapsed
    seconds is allowed and is not that — it reports how long you have waited and predicts nothing —
    so what is forbidden here is precisely a timer that writes stage state.
    """
    stage_writers = re.compile(r"setStages|stageStates\s*\(|states\[[^]]+\]\s*=")
    for name in ("ProgressStepper.tsx", "Ask.tsx"):
        code = Path(ROOT, "frontend/src/features/ask", name).read_text(encoding="utf-8")
        # Comments explain the rule and therefore name what it forbids; only code is scanned.
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)

        for timer in re.finditer(r"set(?:Interval|Timeout)\s*\(", code):
            # The callback is the first argument: read to the comma that ends it.
            tail = code[timer.end() : timer.end() + 400]
            callback = tail.split("),")[0]
            assert not stage_writers.search(callback), (
                f"{name}: a timer callback writes stage state: {callback.strip()!r}"
            )

        assert "percent" not in code.lower(), f"{name}: no progress percentage may be invented"
        assert "estimat" not in code.lower(), f"{name}: no completion estimate may be invented"
        assert "eta" not in re.sub(r"[A-Za-z]eta|eta[A-Za-z]", "", code.lower()), (
            f"{name}: no completion time may be predicted"
        )

    # And the states the interface shows are exactly the vocabulary the server can send.
    shown = Path(ROOT, "frontend/src/features/ask/ProgressStepper.tsx").read_text(encoding="utf-8")
    for state in ("COMPLETED", "RUNNING", "PENDING", "SKIPPED", "FAILED"):
        assert state in shown
