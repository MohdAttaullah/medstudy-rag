"""The lease heartbeat during a long parse, against real PostgreSQL.

Worker-loss recovery itself already existed and is covered by `test_m2_integration.py`: an
expired lease becomes a FAILED run with `PARSER_LEASE_EXPIRED` and a bounded retry. What was
missing was *timeliness* — the lease was renewed only at stage boundaries, so a worker killed
during a multi-minute conversion left the job showing PARSING until the whole lease elapsed.
These tests cover the heartbeat that closes that gap.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from app.core.parsing_config import ParsingConfig
from app.models.enums import Status
from app.models.parsing import ParseRun
from tests.test_m1_integration import auth, database  # noqa: F401, F811
from tests.test_m2_integration import (  # noqa: F401, F811
    Recorded,
    make_service,
    parsed_stub,
    queue_job,
    run_of,
    system_module,
    uuid_of,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires PostgreSQL"
    ),
]


class Windowed(Recorded):
    """A parser that reports progress the way the windowed Docling path does."""

    def __init__(self, windows: int = 4) -> None:
        super().__init__(parsed_stub())
        self.windows = windows
        self.observed: list[tuple[int, int]] = []

    def parse(self, source, config, on_progress=None):  # type: ignore[override]
        total = self.windows * 25
        for index in range(self.windows):
            if on_progress is not None:
                on_progress((index + 1) * 25, total)
                self.observed.append(((index + 1) * 25, total))
        return super().parse(source, config)


def test_a_long_parse_renews_its_lease_between_windows(system_module):  # noqa: F811
    """The lease must track liveness, not a worst-case duration.

    A parse that reports progress keeps pushing its expiry out, so a lease can be configured
    short enough to detect a dead worker quickly without falsely reaping a healthy slow one.
    """
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    parser = Windowed(windows=4)
    # A lease far shorter than a real book parse: only heartbeating keeps it alive.
    config = ParsingConfig(timeout_seconds=30, lease_seconds=60)
    service = make_service(control, config=config, parser_impl=parser)

    before = datetime.now(UTC)
    outcome = service.run(uuid_of(body["job_id"]))
    assert outcome.status is Status.READY_FOR_CHUNKING, outcome

    assert parser.observed == [(25, 100), (50, 100), (75, 100), (100, 100)]
    with control.sessions() as session:
        run = session.get(ParseRun, outcome.parse_run_id)
        assert run.heartbeat_at is not None
        assert run.heartbeat_at >= before, "the run heartbeat advanced during parsing"


def test_a_parser_that_reports_no_progress_still_succeeds(system_module):  # noqa: F811
    """The callback is an optional extension; a single-pass parser must be unaffected."""
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    service = make_service(control, parser_impl=Recorded(parsed_stub()))
    outcome = service.run(uuid_of(body["job_id"]))
    assert outcome.status is Status.READY_FOR_CHUNKING


def test_a_heartbeat_keeps_the_reaper_from_taking_a_live_parse(system_module):  # noqa: F811
    """A healthy slow parse must not be reaped out from under itself."""
    from app.services.parsing import reap_expired_leases

    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    config = ParsingConfig(timeout_seconds=30, lease_seconds=300)
    service = make_service(control, config=config, parser_impl=Windowed(windows=2))
    claim = service._claim(uuid_of(body["job_id"]), None)
    assert claim.status is Status.PARSING

    with control.sessions.begin() as session:
        run = session.get(ParseRun, claim.parse_run_id)
        service._beat(run)  # what the windowed parse does between windows

    assert reap_expired_leases(control.sessions, config) == 0
    assert run_of(control, body["version_id"]).status.name == "RUNNING"

    # And an expiry that is genuinely past is still reaped, so the heartbeat has not disabled it.
    with control.sessions.begin() as session:
        stale = session.get(ParseRun, claim.parse_run_id)
        stale.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert reap_expired_leases(control.sessions, config) == 1
    assert run_of(control, body["version_id"]).error_code == "PARSER_LEASE_EXPIRED"
