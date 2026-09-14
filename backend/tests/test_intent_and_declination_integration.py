"""Both refusals over the real pipeline: one before retrieval, one after generation.

The unit tests pin the classifier and the schema. These pin what the reader actually receives, and
the two properties that make each refusal safe rather than merely tidy — that an out-of-scope
question reaches no index and no provider at all, and that a declined draft reaches no verifier,
carries no answer and carries no citations.
"""

import os
from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.generation.providers.fake import FakeProvider
from app.services.ask import AskService
from app.services.evidence import EvidenceService
from app.services.generation import GenerationService
from app.services.verification import VerificationService
from app.sufficiency.gate import SufficiencyGate
from app.verification.verifier import FakeClaimVerifier
from tests.test_m1_integration import auth, database  # noqa: F401, F811
from tests.test_m2_integration import system_module as system_module
from tests.test_m4_integration import chunked, qdrant  # noqa: F401, F811
from tests.test_m5_integration import (  # noqa: F401, F811
    StubQueryEncoder,
    build_sparse,
    indexed,  # noqa: F811
    principal,
    retrieval_service,
)
from tests.test_m6_integration import StubReranker
from tests.test_m7_integration import declining
from tests.test_m8_integration import responder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

ACADEMIC = "What do the synthetic table and formula sources state?"
PERSONAL = "What antibiotic should I take?"


@pytest.fixture
def stack(indexed, qdrant):  # noqa: F811 - pytest fixture imports
    build_sparse(indexed)
    client, control, credentials, body, _, _ = indexed
    retrieval = retrieval_service(control, StubQueryEncoder(control.settings.query_encoder), qdrant)
    control.evidence = EvidenceService(retrieval, control.settings, StubReranker())
    permissive = SufficiencyConfig(
        ordinary=EvidenceRequirement(),
        table=EvidenceRequirement(),
        formula=EvidenceRequirement(),
    )
    settings = control.settings.model_copy(update={"sufficiency": permissive})
    provider = FakeProvider(responder())
    control.generation = GenerationService(control.evidence, settings, provider)
    verifier = FakeClaimVerifier()
    control.verification = VerificationService(control.generation, settings, verifier, provider)
    control.ask = AskService(control.verification, settings)
    return client, control, credentials, provider, verifier


def ask(client, credentials, question):
    return client.post(
        "/api/v1/ask",
        headers=auth(credentials),
        json={"question": question, "idempotency_key": uuid4().hex},
    )


# ------------------------------------------------------------- refused before retrieval


def test_a_personal_clinical_question_is_refused_without_searching_or_generating(stack):
    client, _, credentials, provider, verifier = stack
    result = ask(client, credentials, PERSONAL)

    assert result.status_code == 200, result.text
    data = result.json()
    assert data["outcome"] == "OUT_OF_SCOPE"
    assert data["reason_codes"] == ["PERSONAL_MEDICAL_ADVICE_REQUESTED"]
    assert data["verified"] is False
    assert data["answer"] is None
    assert data["citations"] == [] and data["sources"] == [] and data["claims"] == []
    # The two that make this a policy refusal rather than an expensive one.
    assert provider.calls == [], "no provider may be called for an out-of-scope question"
    assert verifier.seen == []


def test_the_refusal_message_is_product_controlled_and_gives_no_clinical_direction(stack):
    client, _, credentials, _, _ = stack
    message = ask(client, credentials, PERSONAL).json()["message"]
    assert "educational" in message.lower()
    assert "clinician" in message.lower()
    # It may tell an urgent reader to seek care; it may not do any of the system's forbidden work.
    for forbidden in ("you should take", "mg", "diagnos", "prescrib", "your dose"):
        assert forbidden not in message.lower()


def test_an_academic_question_is_untouched_by_the_intent_check(stack):
    client, _, credentials, provider, _ = stack
    data = ask(client, credentials, ACADEMIC).json()
    assert data["outcome"] != "OUT_OF_SCOPE"
    assert provider.calls, "an academic question must still reach generation"


def test_the_refusal_is_recorded_and_replays_from_the_idempotency_key(stack):
    client, _, credentials, _, _ = stack
    key = uuid4().hex
    body = {"question": PERSONAL, "idempotency_key": key}
    first = client.post("/api/v1/ask", headers=auth(credentials), json=body).json()
    second = client.post("/api/v1/ask", headers=auth(credentials), json=body).json()
    assert first["turn_id"] == second["turn_id"]
    assert second["outcome"] == "OUT_OF_SCOPE"


# ------------------------------------------------------------ declined after generation


def test_a_declined_draft_abstains_semantically_rather_than_failing(stack):
    client, control, credentials, _, verifier = stack
    control.generation._provider = FakeProvider(lambda q, e: declining())
    control.verification._provider = control.generation._provider

    data = ask(client, credentials, ACADEMIC).json()
    assert data["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "EVIDENCE_DOES_NOT_ADDRESS_QUESTION" in data["reason_codes"]
    # The bug this replaces: a correct refusal reported as a broken service.
    assert "GENERATION_SCHEMA_VIOLATION" not in data["reason_codes"]
    assert data["outcome"] != "FAILED"


def test_a_declined_draft_releases_no_answer_and_no_citations(stack):
    client, control, credentials, _, _ = stack
    control.generation._provider = FakeProvider(lambda q, e: declining())
    control.verification._provider = control.generation._provider

    data = ask(client, credentials, ACADEMIC).json()
    assert data["verified"] is False
    assert data["answer"] is None
    assert data["citations"] == [] and data["sources"] == [] and data["claims"] == []


def test_a_declined_draft_is_never_sent_to_claim_verification(stack):
    """There is no draft, so there is nothing for M8 to check — and it must not be asked to."""
    client, control, credentials, _, verifier = stack
    control.generation._provider = FakeProvider(lambda q, e: declining())
    control.verification._provider = control.generation._provider

    ask(client, credentials, ACADEMIC)
    assert verifier.seen == []


def test_the_gate_still_runs_before_the_provider_can_decline(stack):
    """Declination is downstream of M7. A refused question never reaches a provider at all."""
    client, control, credentials, _, _ = stack
    # This question classifies as table/formula dependent, so setting only `ordinary` would leave
    # the requirement the gate actually consults untouched.
    unreachable = EvidenceRequirement(min_supporting_blocks=20)
    impossible = SufficiencyConfig(
        ordinary=unreachable, table=unreachable, formula=unreachable, figure=unreachable
    )
    # The gate is built once in the service constructor, so replacing it is what takes effect;
    # reassigning `settings` alone would leave the permissive gate in place and prove nothing.
    control.generation.gate = SufficiencyGate(impossible)
    provider = FakeProvider(lambda q, e: declining())
    control.generation._provider = provider
    control.verification._provider = provider

    data = ask(client, credentials, ACADEMIC).json()
    assert data["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "INSUFFICIENT_SUPPORTING_BLOCKS" in data["reason_codes"]
    assert "EVIDENCE_DOES_NOT_ADDRESS_QUESTION" not in data["reason_codes"]
    assert provider.calls == []


def test_an_ordinary_draft_still_reaches_verification_unchanged(stack):
    client, _, credentials, provider, verifier = stack
    data = ask(client, credentials, ACADEMIC).json()
    assert provider.calls, "generation ran"
    assert verifier.seen, "M8 still verifies a non-declined draft"
    assert data["outcome"] in {"VERIFIED", "UNVERIFIED"}


def test_a_malformed_provider_response_is_still_a_schema_violation(stack):
    """Declining is representable now; being incoherent is still a failure."""
    client, control, credentials, _, _ = stack
    broken = FakeProvider(lambda q, e: {"result": {"outcome": "DECLINED"}})
    control.generation._provider = broken
    control.verification._provider = broken

    data = ask(client, credentials, ACADEMIC).json()
    assert data["outcome"] == "FAILED"
    assert data["reason_codes"] == ["GENERATION_SCHEMA_VIOLATION"]
