"""Reviewed parse acceptance against real PostgreSQL, including the database guards.

A flagged parse is produced the same way M2 already produces one — a threshold no real page can
meet — so no large fixture is needed. What is asserted here is that acceptance admits *that exact
dataset* while leaving the automated verdict and every finding untouched, and that the database
refuses every shortcut around it.
"""

import os
from uuid import uuid4

import pytest
from app.core.parsing_config import ParseThresholds, ParsingConfig
from app.models.chunking import Chunk, ChunkRun
from app.models.documents import IngestionJob
from app.models.enums import ParseResult, ParseRunStatus, Status
from app.models.parsing import ParseReviewDecision, ParseValidationFinding
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from tests.test_m1_integration import auth, database  # noqa: F401, F811
from tests.test_m2_integration import (  # noqa: F401, F811
    Recorded,
    artifact_stub,
    auth,
    make_service,
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

RATIONALE = "Reviewed both flagged pages against the source; content is present and attributable."
FLAGGING = ParsingConfig(thresholds=ParseThresholds(min_chars_per_page=10000))
#: A different policy fingerprint, so a reparse genuinely produces a new run rather than
#: reusing the existing one. Still a threshold no real page can meet.
FLAGGING_AGAIN = ParsingConfig(thresholds=ParseThresholds(min_chars_per_page=9999))


def flagged(client, control, credentials):
    """A real parse the deterministic quality layer flagged for review."""
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    outcome = make_service(control, config=FLAGGING).run(uuid_of(body["job_id"]))
    assert outcome.status is Status.NEEDS_REVIEW
    run = run_of(control, body["version_id"])
    assert run.status is ParseRunStatus.FAILED and not run.is_active
    return body, run


#: A flagged parse whose *content* is still rich enough to chunk. The stub artifact carries one
#: page of 400 source characters against far less parsed text, so a 300-character floor raises
#: PAGE_CONTENT_LOST — an ERROR, hence NEEDS_REVIEW — over a document M3 already proves chunkable.
CHUNKABLE_FLAGGING = ParsingConfig(thresholds=ParseThresholds(min_chars_per_page=300))


def flagged_but_chunkable(client, control, credentials):
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    outcome = make_service(
        control, config=CHUNKABLE_FLAGGING, parser_impl=Recorded(artifact_stub())
    ).run(uuid_of(body["job_id"]))
    assert outcome.status is Status.NEEDS_REVIEW, outcome
    return body, run_of(control, body["version_id"])


def base_of(body) -> str:
    return f"/api/v1/documents/{body['document_id']}/versions/{body['version_id']}"


def accept(client, token, body, run, rationale=RATIONALE):
    return client.post(
        f"{base_of(body)}/parse-runs/{run.id}/review",
        headers={"Authorization": "Bearer " + token},
        json={"decision": "ACCEPT", "rationale": rationale},
    )


def findings_of(control, run_id):
    with control.sessions() as session:
        return sorted(
            (f.code, f.severity, f.page_number, f.message)
            for f in session.scalars(
                select(ParseValidationFinding).where(ParseValidationFinding.parse_run_id == run_id)
            )
        )


def test_acceptance_admits_the_parse_without_rewriting_the_verdict(system_module):  # noqa: F811
    """The whole model in one assertion set: usable downstream, still visibly flagged."""
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    before = findings_of(control, run.id)
    assert before, "the fixture must actually produce findings"

    response = accept(client, credentials[0].token.get_secret_value(), body, run)
    assert response.status_code == 200, response.text
    decision = response.json()

    with control.sessions() as session:
        session.expire_all()
        reloaded = run_of(control, body["version_id"])
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
    assert reloaded.status is ParseRunStatus.REVIEWED_ACCEPTED
    assert reloaded.is_active, "the accepted dataset becomes the active one"
    assert reloaded.validation_result is ParseResult.NEEDS_REVIEW, "the verdict is never rewritten"
    assert job.status is Status.READY_FOR_CHUNKING
    assert findings_of(control, run.id) == before, "findings are untouched by acceptance"
    assert decision["decision"] == "ACCEPT"
    assert decision["validation_result_at_decision"] == "NEEDS_REVIEW"
    assert decision["finding_count"] == len(before)
    assert decision["rationale"] == RATIONALE


def test_acceptance_does_not_consume_a_retry(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    with control.sessions() as session:
        before = session.get(IngestionJob, uuid_of(body["job_id"])).retry_count

    assert accept(client, credentials[0].token.get_secret_value(), body, run).status_code == 200

    with control.sessions() as session:
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
    assert job.retry_count == before, "review is not reprocessing and must not spend the budget"


def test_acceptance_creates_no_second_parse_run(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    assert accept(client, credentials[0].token.get_secret_value(), body, run).status_code == 200
    with control.sessions() as session:
        runs = list(
            session.scalars(
                select(ParseValidationFinding.parse_run_id)
                .where(ParseValidationFinding.parse_run_id == run.id)
                .distinct()
            )
        )
    assert runs == [run.id], "the accepted dataset is the one the curator examined"


def test_a_second_acceptance_is_idempotent(system_module):  # noqa: F811
    """A retried request, or a second curator, must not produce a second decision."""
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    token = credentials[0].token.get_secret_value()
    first = accept(client, token, body, run)
    second = accept(client, token, body, run, rationale="A different rationale entirely, typed.")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["rationale"] == RATIONALE, "the original decision stands"

    with control.sessions() as session:
        decisions = list(
            session.scalars(
                select(ParseReviewDecision).where(ParseReviewDecision.parse_run_id == run.id)
            )
        )
    assert len(decisions) == 1


def test_the_decision_is_append_only(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    assert accept(client, credentials[0].token.get_secret_value(), body, run).status_code == 200

    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        decision = session.scalar(
            select(ParseReviewDecision).where(ParseReviewDecision.parse_run_id == run.id)
        )
        decision.rationale = "rewritten"
        session.flush()


def test_the_database_refuses_acceptance_without_a_decision(system_module):  # noqa: F811
    """The invariant, tested against the trigger rather than through the service."""
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)

    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        stored = session.get(type(run), run.id)
        stored.status = ParseRunStatus.REVIEWED_ACCEPTED
        session.flush()


def test_the_database_refuses_rewriting_an_automated_verdict(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)

    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        stored = session.get(type(run), run.id)
        stored.validation_result = ParseResult.PASS
        session.flush()


def test_a_reader_cannot_accept_a_parse(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    response = accept(client, credentials[1].token.get_secret_value(), body, run)
    assert response.status_code == 403


def test_another_tenant_cannot_see_or_accept_the_parse(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    response = accept(client, credentials[2].token.get_secret_value(), body, run)
    assert response.status_code == 404, "not found, never forbidden, across a tenant boundary"


def test_an_unauthenticated_request_is_refused(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    response = client.post(
        f"{base_of(body)}/parse-runs/{run.id}/review",
        json={"decision": "ACCEPT", "rationale": RATIONALE},
    )
    assert response.status_code == 401


def test_a_passing_parse_is_not_reviewable(system_module):  # noqa: F811
    """Nothing to accept: the automatic path never needed a human."""
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    outcome = make_service(control).run(uuid_of(body["job_id"]))
    assert outcome.status is Status.READY_FOR_CHUNKING
    run = run_of(control, body["version_id"])
    response = accept(client, credentials[0].token.get_secret_value(), body, run)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PARSE_REVIEW_NOT_APPLICABLE"


def test_an_empty_rationale_is_refused(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    response = accept(client, credentials[0].token.get_secret_value(), body, run, rationale="  ")
    assert response.status_code == 422


def test_an_unknown_run_is_not_found(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, _ = flagged(client, control, credentials)
    response = client.post(
        f"{base_of(body)}/parse-runs/{uuid4()}/review",
        headers={"Authorization": "Bearer " + credentials[0].token.get_secret_value()},
        json={"decision": "ACCEPT", "rationale": RATIONALE},
    )
    assert response.status_code == 404


def test_the_decision_is_readable_afterwards_and_absent_before(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    headers = {"Authorization": "Bearer " + credentials[0].token.get_secret_value()}
    path = f"{base_of(body)}/parse-runs/{run.id}/review"

    assert client.get(path, headers=headers).json() is None
    assert accept(client, credentials[0].token.get_secret_value(), body, run).status_code == 200
    recorded = client.get(path, headers=headers).json()
    assert recorded["decision"] == "ACCEPT"
    assert recorded["reviewer_user_id"] == str(credentials[0].user_id)
    assert recorded["findings_digest"] and recorded["correlation_id"]


def test_acceptance_is_audited_without_leaking_the_rationale(system_module):  # noqa: F811
    """Rationale is curator prose that may quote the document; audit metadata stays safe."""
    from app.models.documents import AuditEvent

    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    assert accept(client, credentials[0].token.get_secret_value(), body, run).status_code == 200

    with control.sessions() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "PARSE_REVIEW_ACCEPTED", AuditEvent.resource_id == run.id
            )
        )
    assert event is not None
    assert event.actor_id == credentials[0].user_id
    assert event.safe_metadata["validation_result"] == "NEEDS_REVIEW"
    assert event.safe_metadata["finding_count"] >= 1
    assert RATIONALE not in str(event.safe_metadata)


def test_acceptance_does_not_carry_over_to_a_later_parse_run(system_module):  # noqa: F811
    """A decision belongs to one exact run; a reparse produces a run with none."""
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    assert accept(client, credentials[0].token.get_secret_value(), body, run).status_code == 200

    headers = {"Authorization": "Bearer " + credentials[0].token.get_secret_value()}
    reparse = client.post(f"/api/v1/ingestion/jobs/{body['job_id']}/reparse", headers=headers)
    assert reparse.status_code == 200, reparse.text
    second = make_service(control, config=FLAGGING_AGAIN).run(uuid_of(body["job_id"]))
    assert second.status is Status.NEEDS_REVIEW

    with control.sessions() as session:
        runs = list(
            session.scalars(
                select(type(run)).where(
                    type(run).document_version_id == uuid_of(body["version_id"])
                )
            )
        )
        decisions = list(session.scalars(select(ParseReviewDecision)))
    newest = max(runs, key=lambda item: item.attempt)
    assert newest.id != run.id
    assert newest.status is ParseRunStatus.FAILED, "the new run is flagged and unaccepted"
    assert all(decision.parse_run_id != newest.id for decision in decisions)


def test_a_superseded_run_cannot_be_accepted_after_a_reparse(system_module):  # noqa: F811
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    headers = {"Authorization": "Bearer " + credentials[0].token.get_secret_value()}
    assert (
        client.post(f"/api/v1/ingestion/jobs/{body['job_id']}/reparse", headers=headers).status_code
        == 200
    )
    make_service(control, config=FLAGGING_AGAIN).run(uuid_of(body["job_id"]))

    response = accept(client, credentials[0].token.get_secret_value(), body, run)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PARSE_REVIEW_SUPERSEDED"


def test_an_identical_reparse_reuses_the_accepted_run_rather_than_unaccepting_it(
    system_module,  # noqa: F811
):
    """M2 treats a reparse with the same parser, policy and source as a no-op.

    Worth pinning here: the run it reuses is the accepted one, so the job returns to chunking on
    the strength of the decision that already exists. No unaccepted run appears, and nothing
    carries over to a run that was never reviewed — because no new run is created at all.
    """
    client, control, credentials = system_module
    body, run = flagged(client, control, credentials)
    assert accept(client, credentials[0].token.get_secret_value(), body, run).status_code == 200

    headers = {"Authorization": "Bearer " + credentials[0].token.get_secret_value()}
    assert (
        client.post(f"/api/v1/ingestion/jobs/{body['job_id']}/reparse", headers=headers).status_code
        == 200
    )
    outcome = make_service(control, config=FLAGGING).run(uuid_of(body["job_id"]))
    assert outcome.skipped == "already_parsed"
    assert outcome.parse_run_id == run.id

    with control.sessions() as session:
        session.expire_all()
        reloaded = run_of(control, body["version_id"])
    assert reloaded.id == run.id
    assert reloaded.status is ParseRunStatus.REVIEWED_ACCEPTED
    assert reloaded.validation_result is ParseResult.NEEDS_REVIEW


# ------------------------------------------------------------------- downstream eligibility


def test_an_accepted_parse_chunks_and_its_dataset_activates(system_module):  # noqa: F811
    """The clause that would have failed silently.

    Chunk-run creation checks the parse `status`, but activation checks `validation_result` — and
    a reviewed parse keeps NEEDS_REVIEW forever. Had only the first been widened, chunking would
    have run to completion and then refused to activate the dataset it had just produced, which is
    a far worse failure than being refused up front. This drives both clauses for real.
    """
    client, control, credentials = system_module
    body, run = flagged_but_chunkable(client, control, credentials)
    try:
        assert accept(client, credentials[0].token.get_secret_value(), body, run).status_code == 200

        # m3_run_guard, INSERT branch: the parse must be an active, usable source.
        chunk_run_id = control.chunks.schedule(uuid_of(body["job_id"]))
        assert chunk_run_id is not None, "a reviewed-accepted parse must be chunkable"
        # m3_run_guard, activation branch: keyed on validation_result, which is still NEEDS_REVIEW.
        control.chunks.run(chunk_run_id)

        with control.sessions() as session:
            chunk_run = session.get(ChunkRun, chunk_run_id)
            chunks = session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.chunk_run_id == chunk_run_id)
            )
            job = session.get(IngestionJob, uuid_of(body["job_id"]))
            reloaded = run_of(control, body["version_id"])
        assert chunk_run.status == "SUCCEEDED", chunk_run.error_code
        assert chunk_run.is_active, "the dataset built from an accepted parse must activate"
        assert chunks and chunks > 0
        assert job.status is Status.READY_FOR_EMBEDDING

        # And the parse it came from is still visibly a reviewed acceptance, not a pass.
        assert reloaded.status is ParseRunStatus.REVIEWED_ACCEPTED
        assert reloaded.validation_result is ParseResult.NEEDS_REVIEW
        assert findings_of(control, run.id), "findings survive the whole downstream path"
    finally:
        client.post(f"/api/v1/ingestion/jobs/{body['job_id']}/cancel", headers=auth(credentials))


def test_a_flagged_parse_nobody_accepted_still_cannot_be_chunked(system_module):  # noqa: F811
    """The other half: widening the guard must not have made it permissive.

    Refusal happens at the stage gate, before the parse predicate is consulted — a job that is
    not READY_FOR_CHUNKING is simply not scheduled — so the review state is left intact rather
    than clobbered into a failure. This is the state the real 932-page textbook sat in, and it
    must stay reachable only through review.
    """
    client, control, credentials = system_module
    body, _ = flagged_but_chunkable(client, control, credentials)

    assert control.chunks.schedule(uuid_of(body["job_id"])) is None
    with control.sessions() as session:
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
        runs = session.scalar(
            select(func.count())
            .select_from(ChunkRun)
            .where(ChunkRun.document_version_id == uuid_of(body["version_id"]))
        )
    assert runs == 0, "no chunk run may exist for a parse nobody reviewed"
    assert job.status is Status.NEEDS_REVIEW, "the job still awaits review, it did not fail"
    assert job.last_error_code == "PARSE_NEEDS_REVIEW"
