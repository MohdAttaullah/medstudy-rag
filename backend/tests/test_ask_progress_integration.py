"""Stage progress over the real pipeline, database and authorized API boundary.

The interface problem this answers: a twelve-second Ask request showed a static list of stage names,
so a reader could not tell which stage was running, which had finished, or whether the machine was
still alive. The fix is only meaningful if the stages come from the pipeline, so what is established
here is that they do — every stage is announced by the code that performs it, in the order the
pipeline runs it — and that the channel cannot carry anything a reader is not allowed to see.

Generator and verifier are the same deterministic doubles M9 uses, so an outcome is chosen by the
test rather than by a provider.
"""

import json
import os
from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.core.verification_config import RepairConfig
from app.generation.errors import GenerationError
from app.generation.providers.fake import FakeProvider
from app.services.progress import STAGES
from app.verification.verifier import FakeClaimVerifier, VerifierVerdict
from tests.test_m1_integration import auth, database  # noqa: F401 - pytest fixture imports
from tests.test_m2_integration import system_module as system_module
from tests.test_m4_integration import chunked, qdrant  # noqa: F401, F811
from tests.test_m5_integration import (  # noqa: F401, F811
    StubQueryEncoder,
    build_sparse,
    indexed,  # noqa: F811
    principal,
    retrieval_service,
)
from tests.test_m9_integration import DRAFT, QUESTION, stack  # noqa: F401 - fixture import

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

PERSONAL = "What antibiotic should I take for meningitis?"
EVENTS = {"accept": "text/event-stream"}


def stream(client, credentials, question=QUESTION, role=0, **extra):
    """Ask for events and return (stage events, the single terminal frame)."""
    response = client.post(
        "/api/v1/ask",
        headers={**auth(credentials, role), **EVENTS},
        json={"question": question, **extra},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    stages, terminal = [], None
    for block in response.text.split("\n\n"):
        if not block.strip():
            continue
        kind = next(
            line[len("event: ") :] for line in block.splitlines() if line.startswith("event: ")
        )
        data = json.loads(
            next(line[len("data: ") :] for line in block.splitlines() if line.startswith("data: "))
        )
        if kind == "stage":
            stages.append(data)
        else:
            terminal = (kind, data)
    assert terminal is not None, "the stream must end with a result or an error"
    return stages, terminal, response.text


def states(stages):
    """Final state per stage, in the order each stage was first announced."""
    final: dict[str, str] = {}
    for event in sorted(stages, key=lambda e: e["sequence"]):
        final[event["stage"]] = event["state"]
    return final


# --------------------------------------------------------- the stages are the pipeline's own


def test_every_stage_is_announced_in_pipeline_order(stack):  # noqa: F811
    client, _, credentials, _, _, _ = stack
    stages, (kind, result), _ = stream(client, credentials)
    assert kind == "result" and result["outcome"] == "VERIFIED"

    order = [event["stage"] for event in stages if event["state"] == "RUNNING"]
    # Announced in the order the pipeline runs them, and every stage of a verified answer runs.
    assert order == list(STAGES)
    assert set(states(stages).values()) == {"COMPLETED"}
    # Sequence numbers are dense and monotonic, so a client can order the timeline itself.
    assert [event["sequence"] for event in stages] == list(range(1, len(stages) + 1))


def test_each_stage_reports_when_it_started_and_how_long_it_took(stack):  # noqa: F811
    client, _, credentials, _, _, _ = stack
    stages, _, _ = stream(client, credentials)
    running = {e["stage"]: e for e in stages if e["state"] == "RUNNING"}
    done = {e["stage"]: e for e in stages if e["state"] == "COMPLETED"}
    for stage in STAGES:
        assert running[stage]["started_at"], f"{stage} announced no start time"
        assert done[stage]["completed_at"], f"{stage} announced no completion time"
        assert done[stage]["elapsed_ms"] is not None and done[stage]["elapsed_ms"] >= 0


def test_a_stage_event_carries_no_content_of_any_kind(stack):  # noqa: F811
    """The channel is metadata only. Nothing a reader may not see can travel on it."""
    client, _, credentials, _, _, _ = stack
    stages, _, _ = stream(client, credentials)
    permitted = {
        "request_id",
        "stage",
        "state",
        "sequence",
        "started_at",
        "completed_at",
        "elapsed_ms",
    }
    for event in stages:
        assert set(event) == permitted, (
            f"unexpected field on a stage event: {set(event) - permitted}"
        )


def test_the_request_id_on_every_event_is_this_request(stack):  # noqa: F811
    client, _, credentials, _, _, _ = stack
    stages, (_, result), _ = stream(client, credentials)
    assert {event["request_id"] for event in stages} == {result["correlation_id"]}


# --------------------------------------------------------- outcomes decide which stages run


def test_out_of_scope_skips_retrieval_and_generation(stack):  # noqa: F811
    """A personal-advice question is refused from the question alone, so nothing else runs."""
    client, _, credentials, _, provider, _ = stack
    stages, (kind, result), _ = stream(client, credentials, question=PERSONAL)
    assert kind == "result" and result["outcome"] == "OUT_OF_SCOPE"
    final = states(stages)
    assert final["PREPARING"] == "COMPLETED"
    assert final["FINALIZE"] == "COMPLETED"
    for skipped in ("RETRIEVAL", "RERANK", "EVIDENCE", "GENERATION", "VERIFICATION"):
        assert final[skipped] == "SKIPPED", f"{skipped} should not have run"
    assert not provider.calls, "a refused question must not reach a provider"


def test_insufficient_evidence_skips_generation_and_verification(stack):  # noqa: F811
    client, control, credentials, _, provider, _ = stack
    demanding = EvidenceRequirement(min_independent_sources=9)
    control.generation.gate.config = SufficiencyConfig(
        ordinary=demanding, table=demanding, formula=demanding
    )
    stages, (kind, result), _ = stream(client, credentials)
    assert kind == "result" and result["outcome"] == "INSUFFICIENT_EVIDENCE"
    final = states(stages)
    # The evidence stages ran and reached a decision; what they gate did not.
    for ran in ("PREPARING", "RETRIEVAL", "RERANK", "EVIDENCE", "FINALIZE"):
        assert final[ran] == "COMPLETED", f"{ran} should have run"
    assert final["GENERATION"] == "SKIPPED" and final["VERIFICATION"] == "SKIPPED"
    assert not provider.calls


def test_an_unverified_draft_completes_verification_and_stays_hidden(stack):  # noqa: F811
    """Verification ran and concluded; concluding against the draft is not a stage failure."""
    client, control, credentials, _, _, _ = stack
    control.verification._verifier = FakeClaimVerifier(
        lambda claim: VerifierVerdict(verdict="UNSUPPORTED")
    )
    control.verification.settings = control.verification.settings.model_copy(
        update={"repair": RepairConfig(enabled=False)}
    )
    stages, (kind, result), raw = stream(client, credentials)
    assert kind == "result" and result["outcome"] == "UNVERIFIED"
    final = states(stages)
    assert final["GENERATION"] == "COMPLETED"
    assert final["VERIFICATION"] == "COMPLETED"
    assert result["answer"] is None
    # Not in the result, and not anywhere in the stream that carried it either.
    assert DRAFT not in raw


def test_a_provider_failure_marks_the_stage_that_failed(stack):  # noqa: F811
    client, control, credentials, _, _, _ = stack
    control.generation._provider = FakeProvider(
        lambda q, e: GenerationError("GENERATION_PROVIDER_UNAVAILABLE")
    )
    control.verification._provider = control.generation._provider
    stages, (kind, result), _ = stream(client, credentials)
    assert kind == "result" and result["outcome"] == "FAILED"
    final = states(stages)
    assert final["GENERATION"] == "FAILED", "the stage that broke must be the one reported broken"
    assert final["RETRIEVAL"] == "COMPLETED" and final["RERANK"] == "COMPLETED"
    # The request still completed and was recorded, so the reader is told what happened.
    assert final["FINALIZE"] == "COMPLETED"
    assert result["reason_codes"]


# --------------------------------------------------------- the contract around the stream


def test_a_refused_question_still_reports_a_real_total(stack):  # noqa: F811
    """A total of zero for a request that took milliseconds is not a measurement.

    The out-of-scope path refuses from the question alone, but the caller still waited for the
    scope check and for the turn to be recorded, so the total has to cover both.
    """
    client, _, credentials, _, _, _ = stack
    _, (_, result), _ = stream(client, credentials, question=PERSONAL)
    timings = {row["stage"]: row["duration_ms"] for row in result["stages"]}
    assert timings["Recording the turn"] > 0
    assert timings["Total"] >= timings["Recording the turn"]


def test_the_total_includes_verification(stack):  # noqa: F811
    """The total is the request boundary, so it cannot be smaller than a stage inside it."""
    client, _, credentials, _, _, _ = stack
    _, (_, result), _ = stream(client, credentials)
    timings = {row["stage"]: row["duration_ms"] for row in result["stages"]}
    assert "Verifying claims" in timings
    assert "Recording the turn" in timings
    assert timings["Total"] >= timings["Verifying claims"]
    assert timings["Total"] >= sum(v for k, v in timings.items() if k != "Total") * 0.9


def test_the_json_form_is_unchanged_when_events_are_not_requested(stack):  # noqa: F811
    client, _, credentials, _, _, _ = stack
    response = client.post("/api/v1/ask", headers=auth(credentials, 0), json={"question": QUESTION})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert data["outcome"] == "VERIFIED" and data["answer"]
    # One JSON object, not a stream of frames. (The body does contain the timing rows, whose own
    # key is "stage"; what must be absent is the event protocol and any RUNNING state.)
    assert "event: stage" not in response.text
    assert "RUNNING" not in response.text


def test_progress_cannot_be_watched_from_another_tenant(stack):  # noqa: F811
    """There is no progress to fetch by id: events exist only on the asking connection.

    A second tenant's conversation is refused before any stage runs, and the refusal arrives as a
    typed error on that caller's own stream — carrying no stage timeline and no answer.
    """
    client, _, credentials, _, provider, _ = stack
    stages, (kind, payload), raw = stream(client, credentials, role=1, conversation_id=str(uuid4()))
    assert kind == "error"
    assert payload["error"]["code"] == "CONVERSATION_NOT_FOUND"
    assert payload["error"]["status"] == 404
    assert not [event for event in stages if event["state"] == "RUNNING"]
    assert not provider.calls
    assert DRAFT not in raw

    # And no route accepts a request id to read somebody else's progress.
    paths = {getattr(route, "path", "") for route in client.app.routes}
    assert not [path for path in paths if "progress" in path or "events" in path]


# --------------------------------------------------------- source figures on an answer


def read(client, credentials, conversation_id, role=0):
    response = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(credentials, role)
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_verified_answer_carries_a_figures_list(stack):  # noqa: F811
    """The field exists on every verified answer, empty when nothing links."""
    client, _, credentials, _, _, _ = stack
    _, (_, result), _ = stream(client, credentials)
    assert result["outcome"] == "VERIFIED"
    assert isinstance(result["figures"], list)
    for figure in result["figures"]:
        # Whatever is listed is linked to a citation of this very answer.
        assert figure["citation_ids"]
        assert {c["citation_id"] for c in result["citations"]} >= set(figure["citation_ids"])
        assert figure["linked_by"] in {"CITED_EVIDENCE", "CITED_TEXT_REFERENCE"}


def test_a_reloaded_conversation_resolves_the_same_figures(stack):  # noqa: F811
    """A figure that appears with the answer must still be there when the turn is read back.

    The two paths build their views separately, so this is the assertion that keeps them from
    drifting — an answer that showed a figure and then lost it on reload would look like the
    evidence had changed.
    """
    client, _, credentials, _, _, _ = stack
    _, (_, result), _ = stream(client, credentials)
    stored = read(client, credentials, result["conversation_id"])
    turn = next(t for t in stored["turns"] if t["turn_id"] == result["turn_id"])
    assert [f["figure_id"] for f in turn["figures"]] == [f["figure_id"] for f in result["figures"]]
    assert [f["linked_by"] for f in turn["figures"]] == [f["linked_by"] for f in result["figures"]]


def test_an_unverified_turn_carries_no_figures(stack):  # noqa: F811
    """No answer was released, so nothing is source material *for* it."""
    client, control, credentials, _, _, _ = stack
    control.verification._verifier = FakeClaimVerifier(
        lambda claim: VerifierVerdict(verdict="UNSUPPORTED")
    )
    control.verification.settings = control.verification.settings.model_copy(
        update={"repair": RepairConfig(enabled=False)}
    )
    _, (_, result), _ = stream(client, credentials)
    assert result["outcome"] == "UNVERIFIED"
    assert result["figures"] == []
    stored = read(client, credentials, result["conversation_id"])
    assert all(turn["figures"] == [] for turn in stored["turns"])


def test_a_figure_never_travels_with_an_object_store_key(stack):  # noqa: F811
    client, _, credentials, _, _, _ = stack
    _, (_, result), raw = stream(client, credentials)
    for leaked in ("image_key", "minio", "x-amz", "9000"):
        assert leaked not in raw.lower()
