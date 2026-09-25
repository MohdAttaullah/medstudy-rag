"""Stage progress for one in-flight Ask request.

A reader watching a twelve-second request needs to know which stage is running, which are done and
that the machine is still alive. The only honest source for that is the pipeline itself, so this
module carries what the orchestrators already know — they measure every one of these stages to
report `durations_ms` — out to the caller while the request is still running.

Two rules shape it.

*Nothing is simulated.* A stage becomes RUNNING when the code that performs it begins and COMPLETED
when that code returns. There is no timer advancing stages, no progress fraction and no estimate;
a percentage would have to be invented, because the work is not divisible in advance.

*An event carries no content.* Only a stage code, a state and timings. Never a draft, a claim, a
verdict, a rationale, a prompt, an evidence text or a score — the fields do not exist on the model,
so a future edit cannot leak one by accident. What a reader may see is decided by M8 and shaped by
`AskResponse`; this channel exists beside that decision and cannot widen it.

The reporter reaches the orchestrators through a `ContextVar` rather than through their signatures.
Retrieval, reranking and evidence assembly run in a worker thread via `run_in_threadpool`, which
copies the context, so the same reporter is visible there; `emit` is therefore written to be safe
to call from a thread other than the one running the event loop.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

#: The stages a reader is shown, in the order the pipeline runs them. Each maps to work that is
#: already measured: PREPARING is the scope/intent check, RETRIEVAL the M5 lanes and their
#: hydration, RERANK the cross-encoder, EVIDENCE expansion and assembly through the sufficiency
#: gate, GENERATION the grounded draft, VERIFICATION claim checking and the contradiction scan,
#: FINALIZE the turn and its citations being recorded.
StageCode = Literal[
    "PREPARING",
    "RETRIEVAL",
    "RERANK",
    "EVIDENCE",
    "GENERATION",
    "VERIFICATION",
    "FINALIZE",
]

STAGES: tuple[StageCode, ...] = (
    "PREPARING",
    "RETRIEVAL",
    "RERANK",
    "EVIDENCE",
    "GENERATION",
    "VERIFICATION",
    "FINALIZE",
)

#: PENDING is the client's initial assumption rather than an emitted state: a stage that has not
#: been announced has not started. The other four are always emitted by the stage itself.
StageState = Literal["PENDING", "RUNNING", "COMPLETED", "SKIPPED", "FAILED"]


class StageEvent(BaseModel):
    """One state change of one stage. Deliberately incapable of carrying content.

    `extra="forbid"` and the absence of any text field are the guarantee: there is nowhere here to
    put a draft sentence, a verifier rationale or an evidence excerpt.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    stage: StageCode
    state: StageState
    sequence: int
    #: Wall-clock, ISO-8601, for a reader who keeps the record. Elapsed time is measured from a
    #: monotonic clock instead, so a clock adjustment mid-request cannot produce a negative stage.
    started_at: str | None = None
    completed_at: str | None = None
    elapsed_ms: float | None = None


class ProgressReporter(Protocol):
    def start(self, stage: StageCode) -> None: ...
    def complete(self, stage: StageCode) -> None: ...
    def skip(self, *stages: StageCode) -> None: ...
    def fail(self, stage: StageCode) -> None: ...


class NullReporter:
    """What the pipeline reports to when nobody is watching.

    The JSON endpoint, the M5-M8 diagnostic routes and every test that calls the services directly
    run against this, so a progress channel that is not connected costs one attribute lookup and
    changes nothing about the pipeline's behaviour.
    """

    def start(self, stage: StageCode) -> None:
        return None

    def complete(self, stage: StageCode) -> None:
        return None

    def skip(self, *stages: StageCode) -> None:
        return None

    def fail(self, stage: StageCode) -> None:
        return None


class QueueReporter:
    """Publishes stage events onto an asyncio queue for one request.

    Ordering is by an integer sequence rather than by arrival: a consumer that reconnected, or a
    client that renders out of order, can still reconstruct the timeline. Duplicate or unknown
    transitions are dropped rather than raising — a progress channel must never be able to fail a
    request it is only describing.
    """

    def __init__(self, request_id: UUID, queue: asyncio.Queue[StageEvent | None]) -> None:
        self._request_id = request_id
        self._queue = queue
        self._loop = asyncio.get_running_loop()
        self._sequence = 0
        self._started: dict[StageCode, float] = {}
        self._begun: dict[StageCode, str] = {}
        self._settled: set[StageCode] = set()

    # ------------------------------------------------------------------ reporting

    def start(self, stage: StageCode) -> None:
        if stage in self._started or stage in self._settled:
            return
        self._started[stage] = perf_counter()
        self._begun[stage] = _now()
        self._emit(stage, "RUNNING", started_at=self._begun[stage])

    def complete(self, stage: StageCode) -> None:
        self._settle(stage, "COMPLETED")

    def fail(self, stage: StageCode) -> None:
        self._settle(stage, "FAILED")

    def skip(self, *stages: StageCode) -> None:
        for stage in stages:
            if stage not in self._settled and stage not in self._started:
                self._settled.add(stage)
                self._emit(stage, "SKIPPED", completed_at=_now())

    def finish(self) -> None:
        """Close the stream. A stage still running when the request ends is reported failed.

        Without this, a request that raised inside a stage would leave that stage spinning in the
        interface forever, which is exactly the "is it stuck?" question this feature exists to
        answer.
        """
        for stage in STAGES:
            if stage in self._started and stage not in self._settled:
                self._settle(stage, "FAILED")
        self._put(None)

    # ------------------------------------------------------------------ internals

    def _settle(self, stage: StageCode, state: StageState) -> None:
        if stage in self._settled:
            return
        self._settled.add(stage)
        started = self._started.pop(stage, None)
        self._emit(
            stage,
            state,
            started_at=self._begun.get(stage),
            completed_at=_now(),
            elapsed_ms=None if started is None else (perf_counter() - started) * 1000,
        )

    def _emit(
        self,
        stage: StageCode,
        state: StageState,
        started_at: str | None = None,
        completed_at: str | None = None,
        elapsed_ms: float | None = None,
    ) -> None:
        self._sequence += 1
        self._put(
            StageEvent(
                request_id=self._request_id,
                stage=stage,
                state=state,
                sequence=self._sequence,
                started_at=started_at,
                completed_at=completed_at,
                elapsed_ms=None if elapsed_ms is None else round(elapsed_ms, 2),
            )
        )

    def _put(self, event: StageEvent | None) -> None:
        # Retrieval, reranking and evidence assembly run in a worker thread, so this is reached
        # from a thread that does not own the loop. Hopping through the loop is what makes that
        # safe; calling `put_nowait` directly from the thread would be a data race.
        try:
            if asyncio.get_running_loop() is self._loop:
                self._queue.put_nowait(event)
                return
        except RuntimeError:
            pass
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)


def _now() -> str:
    return datetime.now(UTC).isoformat()


#: One shared instance is enough because it holds no state; the ContextVar default is None so that
#: nothing mutable is shared between requests by accident.
DISCARD = NullReporter()

_CURRENT: ContextVar[ProgressReporter | None] = ContextVar("ask_progress", default=None)


def reporter() -> ProgressReporter:
    """The reporter for the request being served, or a reporter that discards everything."""
    return _CURRENT.get() or DISCARD


@contextmanager
def reporting(active: ProgressReporter) -> Iterator[ProgressReporter]:
    token = _CURRENT.set(active)
    try:
        yield active
    finally:
        _CURRENT.reset(token)
