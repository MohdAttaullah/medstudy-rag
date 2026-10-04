"""Ask must say *why* it could not answer, and must never call a deliberate refusal an outage.

The defect this guards against was found in manual testing. A reader pasted a 49-word clinical
vignette. The query encoder refuses anything over its 64-token limit — deliberately, and without
truncating, because a shortened question would retrieve evidence for a different question (M5). Ask
caught that declared refusal in the same branch as a crashed provider:

    outcome = "FAILED" if exc.code in PROVIDER_FAILURES else "FAILED"

— both arms identical — so the reader was told "The answering service could not complete this
request. This is a technical failure", in about 0.2 seconds, with a Retry that could only fail the
same way. Nothing was broken. The question was too long, and nothing said so.

The same branch had a second victim that manual testing did not reach: a workspace whose documents
are all still being processed has an empty corpus, and Ask reported that as a technical failure
too, although the true and provable statement is that there was nothing indexed to search.

What is asserted here is the reader-facing contract over the real pipeline, database and API:

* a refusal caused by the question itself says so, and does not claim the service broke;
* an empty corpus is an abstention about the evidence, not an outage;
* a genuine infrastructure failure is still reported as one — this fix must not launder real
  failures into something softer;
* none of these paths ever releases an answer, a draft or a citation.
"""

import os
from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.generation.errors import GenerationError
from app.generation.providers.fake import FakeProvider
from app.retrieval.errors import RetrievalError
from app.services.ask import AskService
from app.services.evidence import EvidenceService
from app.services.generation import GenerationService
from app.services.verification import VerificationService
from app.verification.verifier import FakeClaimVerifier
from tests.test_ask_progress_integration import states, stream
from tests.test_m1_integration import auth, database  # noqa: F401 - pytest fixture imports
from tests.test_m2_integration import system_module as system_module
from tests.test_m4_integration import chunked, qdrant  # noqa: F401, F811
from tests.test_m5_integration import (  # noqa: F401, F811
    StubQueryEncoder,
    build_sparse,
    indexed,  # noqa: F811
    retrieval_service,
)
from tests.test_m6_integration import StubReranker
from tests.test_m8_integration import responder
from tests.test_m9_integration import stack  # noqa: F401 - fixture import

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

# The vignette from the manual test, verbatim. Its length is the point: 49 words.
VIGNETTE = (
    "A 43-year-old man fell onto his left hand and initially had anatomical snuffbox tenderness. "
    "No scaphoid fracture was seen on the initial or subsequent X-rays. He has been immobilized in "
    "a futura splint for two weeks and is now asymptomatic. What is the most appropriate next step "
    "in management?"
)
TECHNICAL = "technical failure"


class OverLongEncoder(StubQueryEncoder):
    """Refuses exactly as the pinned MedCPT encoder refuses an over-limit question."""

    def encode_queries(self, queries):
        raise RetrievalError(
            "QUERY_TOO_LONG",
            {"limit": self.config.max_query_tokens, "observed": 77, "positions": [0]},
        )


def rewire(control, vectors, encoder):
    """The M9 stack with a different query encoder, everything else unchanged."""
    retrieval = retrieval_service(control, encoder, vectors)
    control.evidence = EvidenceService(retrieval, control.settings, StubReranker())
    permissive = SufficiencyConfig(
        ordinary=EvidenceRequirement(), table=EvidenceRequirement(), formula=EvidenceRequirement()
    )
    settings = control.settings.model_copy(update={"sufficiency": permissive})
    provider = FakeProvider(responder())
    control.generation = GenerationService(control.evidence, settings, provider)
    control.verification = VerificationService(
        control.generation, settings, FakeClaimVerifier(), provider
    )
    control.ask = AskService(control.verification, settings)


def assert_nothing_released(result):
    assert result["verified"] is False
    assert result["answer"] is None
    assert result["citations"] == [] and result["sources"] == [] and result["figures"] == []


# ------------------------------------------------------------ a refusal caused by the question


def test_a_question_too_long_to_encode_is_not_reported_as_a_service_failure(stack, qdrant):  # noqa: F811
    client, control, credentials, _, _, _ = stack
    rewire(control, qdrant, OverLongEncoder(control.settings.query_encoder))

    stages, (kind, result), _ = stream(client, credentials, question=VIGNETTE)

    assert kind == "result", "a refusal is a completed request, not an error frame"
    assert result["reason_codes"] == ["QUERY_TOO_LONG"]
    # The reader is told the true cause and what to do about it — and is *not* told that the
    # service broke, because it did not.
    message = result["message"].lower()
    assert TECHNICAL not in message
    assert "shorter" in message or "briefly" in message
    assert "not shortened" in message or "not truncated" in message
    assert_nothing_released(result)
    # The stage that refused is the one reported, and no later stage pretends to have run.
    final = states(stages)
    assert final["RETRIEVAL"] == "FAILED"
    assert "RERANK" not in final and "GENERATION" not in final


def test_the_same_refusal_reads_the_same_when_the_conversation_is_reloaded(stack, qdrant):  # noqa: F811
    """A stored turn is rendered from what was persisted, so the message must be derived from the
    stored reason, not only from the live request that produced it."""
    client, control, credentials, _, _, _ = stack
    rewire(control, qdrant, OverLongEncoder(control.settings.query_encoder))
    _, (_, live), _ = stream(client, credentials, question=VIGNETTE)

    stored = client.get(
        f"/api/v1/conversations/{live['conversation_id']}", headers=auth(credentials)
    ).json()
    turn = stored["turns"][-1]
    assert turn["reason_codes"] == ["QUERY_TOO_LONG"]
    assert turn["message"] == live["message"]
    assert TECHNICAL not in turn["message"].lower()


def test_an_over_long_question_is_still_refused_not_truncated(stack, qdrant):  # noqa: F811
    """The fix is to the wording. The refusal itself — reject, never shorten — is M5 policy and
    must survive: no evidence may be retrieved for a question other than the one asked."""
    client, control, credentials, _, _, _ = stack
    encoder = OverLongEncoder(control.settings.query_encoder)
    rewire(control, qdrant, encoder)
    stages, (_, result), _ = stream(client, credentials, question=VIGNETTE)
    assert result["outcome"] == "FAILED"
    assert_nothing_released(result)
    assert states(stages)["RETRIEVAL"] == "FAILED"


# ------------------------------------------------------------ a corpus with nothing ready in it


def test_a_workspace_with_nothing_ready_abstains_rather_than_failing(stack):  # noqa: F811
    """Credential 2 is a separate tenant with no retrieval-ready version — the state of a new
    workspace whose first upload is still being parsed. Its corpus is empty by construction.

    There is nothing to search, so "the indexed sources do not support an answer" is literally
    true, and it is the statement the system can prove without guessing which document the
    question depended on."""
    client, _, credentials, _, _, _ = stack

    stages, (kind, result), _ = stream(client, credentials, role=2)

    assert kind == "result"
    assert result["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert result["reason_codes"] == ["RETRIEVAL_CORPUS_EMPTY"]
    assert TECHNICAL not in result["message"].lower()
    assert_nothing_released(result)
    # No provider was reached: there was no evidence to draft from.
    final = states(stages)
    assert "GENERATION" not in final or final["GENERATION"] in ("SKIPPED", "PENDING")


def test_a_workspace_with_nothing_ready_cannot_see_another_tenants_corpus(stack):  # noqa: F811
    """The tenant with the indexed corpus is right beside it. An empty corpus must stay empty; it
    must never be satisfied by falling back to someone else's documents."""
    client, _, credentials, _, _, _ = stack
    _, (_, own), _ = stream(client, credentials)
    _, (_, other), _ = stream(client, credentials, role=2)
    assert own["outcome"] == "VERIFIED" and own["citations"]
    assert other["outcome"] == "INSUFFICIENT_EVIDENCE" and other["citations"] == []


# ------------------------------------------------------------ real failures stay real


def test_a_genuine_provider_failure_is_still_a_technical_failure(stack):  # noqa: F811
    """The correction must not launder a real outage into something softer."""
    client, control, credentials, _, _, _ = stack
    control.generation._provider = FakeProvider(
        lambda q, e: GenerationError("GENERATION_PROVIDER_UNAVAILABLE")
    )
    control.verification._provider = control.generation._provider

    _, (_, result), _ = stream(client, credentials)

    assert result["outcome"] == "FAILED"
    assert result["reason_codes"] == ["GENERATION_PROVIDER_UNAVAILABLE"]
    assert TECHNICAL in result["message"].lower()
    assert_nothing_released(result)


def test_an_undeclared_retrieval_failure_is_still_a_technical_failure(stack, qdrant):  # noqa: F811
    """A retrieval code outside the small set of question-caused refusals keeps the technical
    wording: an encoder that is unavailable is the service's problem, not the reader's."""
    client, control, credentials, _, _, _ = stack

    class Unavailable(StubQueryEncoder):
        def encode_queries(self, queries):
            raise RetrievalError("QUERY_ENCODER_UNAVAILABLE")

    rewire(control, qdrant, Unavailable(control.settings.query_encoder))
    _, (_, result), _ = stream(client, credentials)
    assert result["outcome"] == "FAILED"
    assert result["reason_codes"] == ["QUERY_ENCODER_UNAVAILABLE"]
    assert TECHNICAL in result["message"].lower()


# ------------------------------------------------------------ nothing else moved


def test_a_supported_question_still_verifies(stack):  # noqa: F811
    client, _, credentials, _, _, _ = stack
    _, (_, result), _ = stream(client, credentials)
    assert result["outcome"] == "VERIFIED" and result["answer"] and result["citations"]


def test_every_refusal_is_recorded_once_in_its_own_conversation(stack, qdrant):  # noqa: F811
    client, control, credentials, _, _, _ = stack
    rewire(control, qdrant, OverLongEncoder(control.settings.query_encoder))
    key = str(uuid4())
    _, (_, first), _ = stream(client, credentials, question=VIGNETTE, idempotency_key=key)
    _, (_, again), _ = stream(client, credentials, question=VIGNETTE, idempotency_key=key)
    # A retry with the same key returns the stored turn rather than re-running the request.
    assert again["turn_id"] == first["turn_id"]
    stored = client.get(
        f"/api/v1/conversations/{first['conversation_id']}", headers=auth(credentials)
    ).json()
    assert len(stored["turns"]) == 1
