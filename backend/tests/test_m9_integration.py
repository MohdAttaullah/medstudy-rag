"""M9 over the real pipeline, database and authorized API boundary.

Generator and verifier are deterministic doubles. What is established here is that the public Ask
endpoint releases an answer only behind an M8 PASS, that every refusal is distinguishable, that a
rejected draft never appears in any response or row, and that a conversation belongs to exactly one
tenant.
"""

import os
from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.core.verification_config import RepairConfig
from app.generation.errors import GenerationError
from app.generation.providers.fake import FakeProvider
from app.models.conversations import ConversationTurn, TurnCitation
from app.services.ask import AskService
from app.services.evidence import EvidenceService
from app.services.generation import GenerationService
from app.services.verification import VerificationService
from app.verification.verifier import FakeClaimVerifier, VerifierVerdict
from sqlalchemy import select, text
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
from tests.test_m8_integration import responder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

QUESTION = "What do the synthetic table and formula sources state?"
DRAFT = "The source states the parameter values."


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
    return client, control, credentials, body, provider, verifier


def ask(client, credentials, role=0, **extra):
    return client.post(
        "/api/v1/ask", headers=auth(credentials, role), json={"question": QUESTION, **extra}
    )


def test_a_verified_answer_reaches_the_user_with_its_citations(stack):
    client, _, credentials, body, _, _ = stack
    result = ask(client, credentials)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["outcome"] == "VERIFIED" and data["verified"] is True
    assert data["answer"] and data["answering_enabled"] is True
    assert data["citations"] and data["sources"]
    for citation in data["citations"]:
        # Every citation resolves into the tenant's own corpus and carries its authority.
        assert citation["document_version_id"] == body["version_id"]
        assert citation["authority_level"] and citation["cited_text"]
        assert citation["parse_run_id"] and citation["pages"]
    assert data["conversation_id"] and data["turn_id"]
    # No internal artefact travels with the public answer.
    for forbidden in ("draft", "sufficiency", "verification", "evidence_set", "reranked"):
        assert forbidden not in data


@pytest.mark.parametrize(
    ("setup", "outcome", "code"),
    [
        ("insufficient", "INSUFFICIENT_EVIDENCE", None),
        ("unverified", "UNVERIFIED", None),
        ("provider", "FAILED", "GENERATION_PROVIDER_UNAVAILABLE"),
    ],
)
def test_every_refusal_is_distinguishable_and_carries_no_answer(stack, setup, outcome, code):
    """A reader must tell missing evidence from a rejected draft from a broken provider."""
    client, control, credentials, _, _, _ = stack
    if setup == "insufficient":
        demanding = EvidenceRequirement(min_independent_sources=9)
        control.generation.gate.config = SufficiencyConfig(
            ordinary=demanding, table=demanding, formula=demanding
        )
    elif setup == "unverified":
        control.verification._verifier = FakeClaimVerifier(
            lambda claim: VerifierVerdict(verdict="UNSUPPORTED")
        )
        control.verification.settings = control.verification.settings.model_copy(
            update={"repair": RepairConfig(enabled=False)}
        )
    else:
        control.generation._provider = FakeProvider(
            lambda q, e: GenerationError("GENERATION_PROVIDER_UNAVAILABLE")
        )
        control.verification._provider = control.generation._provider

    data = ask(client, credentials).json()
    assert data["outcome"] == outcome
    assert data["verified"] is False
    # The rejected draft never appears, under any key.
    assert data["answer"] is None
    assert DRAFT not in str(data)
    assert not data["citations"] and not data["sources"]
    assert data["message"]
    if code:
        assert code in data["reason_codes"]


def test_a_rejected_draft_is_never_stored_as_an_answer(stack):
    client, control, credentials, _, _, _ = stack
    control.verification._verifier = FakeClaimVerifier(
        lambda claim: VerifierVerdict(verdict="UNSUPPORTED")
    )
    control.verification.settings = control.verification.settings.model_copy(
        update={"repair": RepairConfig(enabled=False)}
    )
    data = ask(client, credentials).json()
    with control.sessions() as session:
        turn = session.scalar(
            select(ConversationTurn).where(ConversationTurn.id == data["turn_id"])
        )
        assert turn is not None
        assert turn.outcome == "UNVERIFIED"
        assert turn.verified is False
        assert turn.answer_text is None
        assert (
            session.scalars(select(TurnCitation).where(TurnCitation.turn_id == turn.id)).all() == []
        )


def test_the_database_refuses_an_answer_on_an_unverified_turn(stack):
    """The safety rule is in the schema, not only in the code that writes to it."""
    _, control, credentials, _, _, _ = stack
    actor = principal(control, credentials)
    with pytest.raises(Exception) as raised:
        with control.sessions.begin() as session:
            session.execute(
                text(
                    "INSERT INTO conversations (id, tenant_id, created_by_user_id, title,"
                    " created_at, updated_at) VALUES (:c, :t, :u, 'x', now(), now())"
                ),
                {"c": (conversation := uuid4()), "t": actor.tenant_id, "u": actor.user_id},
            )
            session.execute(
                text(
                    "INSERT INTO conversation_turns (id, tenant_id, conversation_id,"
                    " created_by_user_id, sequence_number, idempotency_key, correlation_id,"
                    " question_text, outcome, verified, answer_text, reason_codes, repair_count,"
                    " durations_ms, created_at, updated_at) VALUES (:i, :t, :c, :u, 1, :k, :r,"
                    " 'q', 'UNVERIFIED', false, 'Take 50 mg twice daily.', '[]', 0, '{}',"
                    " now(), now())"
                ),
                {
                    "i": uuid4(),
                    "t": actor.tenant_id,
                    "c": conversation,
                    "u": actor.user_id,
                    "k": uuid4().hex,
                    "r": uuid4(),
                },
            )
    assert "answer_only_when_verified" in str(raised.value)


def test_a_retried_submission_returns_the_stored_turn_without_a_second_provider_call(stack):
    client, _, credentials, _, provider, _ = stack
    key = uuid4().hex
    first = ask(client, credentials, idempotency_key=key).json()
    calls = len(provider.calls)
    second = ask(client, credentials, idempotency_key=key).json()
    assert second["turn_id"] == first["turn_id"]
    assert second["answer"] == first["answer"]
    assert len(provider.calls) == calls, "a retry must not spend another provider call"


def test_a_conversation_accumulates_turns_and_is_readable(stack):
    client, _, credentials, _, _, _ = stack
    first = ask(client, credentials).json()
    second = ask(client, credentials, conversation_id=first["conversation_id"]).json()
    assert second["conversation_id"] == first["conversation_id"]

    view = client.get(
        f"/api/v1/conversations/{first['conversation_id']}", headers=auth(credentials)
    )
    assert view.status_code == 200
    turns = view.json()["turns"]
    assert [t["sequence_number"] for t in turns] == [1, 2]
    assert all(t["verified"] for t in turns)

    listing = client.get("/api/v1/conversations", headers=auth(credentials)).json()
    assert listing["total"] >= 1
    assert any(c["conversation_id"] == first["conversation_id"] for c in listing["items"])


def test_another_tenant_cannot_read_or_continue_a_conversation(stack):
    """Ownership comes from the authenticated principal, never from the id in the URL."""
    client, control, credentials, _, _, _ = stack
    mine = ask(client, credentials).json()["conversation_id"]

    actor = principal(control, credentials)
    intruder = type(actor)(
        user_id=uuid4(), tenant_id=uuid4(), display_name="Other tenant", role=actor.role
    )
    with control.sessions() as session:
        from app.repositories.conversations import ConversationRepository

        repository = ConversationRepository(session, intruder.tenant_id, intruder.user_id)
        with pytest.raises(Exception) as raised:
            repository.get(mine)
        # Indistinguishable from an id that never existed, so probing reveals nothing.
        assert "CONVERSATION_NOT_FOUND" in str(raised.value)

    unknown = client.get(f"/api/v1/conversations/{uuid4()}", headers=auth(credentials))
    assert unknown.status_code == 404


def test_continuing_an_unknown_conversation_fails_closed(stack):
    client, _, credentials, _, provider, _ = stack
    response = ask(client, credentials, conversation_id=str(uuid4()))
    assert response.status_code == 404
    assert not provider.calls, (
        "no provider call is made for a conversation that is not the caller's"
    )


def test_unauthenticated_access_is_rejected(stack):
    client, _, _, _, provider, _ = stack
    assert client.post("/api/v1/ask", json={"question": QUESTION}).status_code == 401
    assert client.get("/api/v1/conversations").status_code == 401
    assert not provider.calls


def test_a_reader_may_ask_but_never_sees_the_inspector(stack):
    """Asking is a reading capability; drafts, lane scores and verdicts are not."""
    client, _, credentials, _, _, _ = stack
    assert ask(client, credentials, role=1).status_code == 200
    for path in ("/api/v1/retrieval/search", "/api/v1/retrieval/draft", "/api/v1/retrieval/answer"):
        assert (
            client.post(path, headers=auth(credentials, 1), json={"query": QUESTION}).status_code
            == 403
        )


@pytest.mark.parametrize(
    "field",
    ["tenant_id", "evidence_ids", "verified", "outcome", "answer", "provider", "system_policy"],
)
def test_a_client_cannot_assert_the_answer_decision(stack, field):
    client, _, credentials, _, provider, _ = stack
    assert ask(client, credentials, **{field: "anything"}).status_code == 422
    assert not provider.calls


def test_no_provider_key_appears_in_any_ask_response(stack):
    client, _, credentials, _, _, _ = stack
    body = ask(client, credentials).text
    for forbidden in ("api_key", "sk-ant", "x-api-key", "Authorization"):
        assert forbidden not in body


def test_earlier_endpoints_still_refuse_to_answer(stack):
    """M9 enabled one new endpoint; it did not turn a retrieval endpoint into an answering one."""
    client, _, credentials, _, _, _ = stack
    for path in ("/api/v1/retrieval/search", "/api/v1/retrieval/rerank"):
        data = client.post(path, headers=auth(credentials), json={"query": QUESTION}).json()
        assert data["answering_enabled"] is False
        assert "answer" not in data
