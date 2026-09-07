"""Configuration changes through HTTP preserve the real M7/M8/M9 decision chain.

Provider doubles make outcomes deterministic; PostgreSQL, corpus hydration, gate and verifier
orchestration are real. The separate live smoke exercises the configured provider adapter.
"""

import os

import pytest
from app.models.conversations import ConversationTurn
from app.verification.verifier import FakeClaimVerifier, VerifierVerdict
from sqlalchemy import select
from tests.test_m1_integration import auth, database  # noqa: F401
from tests.test_m2_integration import system_module  # noqa: F401
from tests.test_m4_integration import chunked, qdrant  # noqa: F401
from tests.test_m5_integration import indexed  # noqa: F401
from tests.test_m9_integration import QUESTION  # noqa: F401
from tests.test_m9_integration import stack as stack
from tests.test_m10_integration import apply, preview

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]


def test_runtime_change_keeps_verified_only_delivery_and_snapshots_history(stack):
    client, control, credentials, _, _, _ = stack
    assert apply(client, credentials, preview(client, credentials)).status_code == 200
    result = client.post("/api/v1/ask", headers=auth(credentials, 1), json={"question": QUESTION})
    assert result.status_code == 200, result.text
    assert result.json()["outcome"] == "VERIFIED" and result.json()["verified"]
    with control.sessions() as session:
        row = session.scalars(
            select(ConversationTurn).where(ConversationTurn.tenant_id == credentials[0].tenant_id)
        ).first()
        assert row.configuration_snapshot["revision"] == 1
        assert row.configuration_snapshot["values"]["retrieval.rrf_k"] == 50
    assert (
        apply(client, credentials, preview(client, credentials, value=70, revision=1)).status_code
        == 200
    )
    with control.sessions() as session:
        old = session.get(ConversationTurn, row.id)
        assert old.configuration_snapshot["values"]["retrieval.rrf_k"] == 50


def test_policy_change_cannot_rescue_a_verifier_failure(stack):
    client, control, credentials, _, _, _ = stack
    control.verification._verifier = FakeClaimVerifier(
        lambda claim: VerifierVerdict(verdict="UNSUPPORTED")
    )
    revision = client.get("/api/v1/settings", headers=auth(credentials)).json()["revision"]
    assert (
        apply(client, credentials, preview(client, credentials, revision=revision)).status_code
        == 200
    )
    result = client.post(
        "/api/v1/ask", headers=auth(credentials, 1), json={"question": QUESTION}
    ).json()
    assert result["outcome"] == "UNVERIFIED" and result["answer"] is None
    assert not result["verified"] and not result["citations"] and not result["sources"]
    for key in (
        "ask.requires_verified_pass",
        "claim_verification.semantic_verification_enabled",
        "claim_verification.verifier_required",
        "sufficiency.incomplete_context_is_insufficient",
    ):
        assert preview(client, credentials, key, False, revision=revision + 1).status_code == 422


def test_stricter_structural_requirement_stops_before_generation(stack):
    client, _, credentials, _, provider, _ = stack
    revision = client.get("/api/v1/settings", headers=auth(credentials)).json()["revision"]
    assert (
        apply(
            client,
            credentials,
            preview(
                client,
                credentials,
                "sufficiency.table.min_independent_sources",
                9,
                revision=revision,
            ),
        ).status_code
        == 200
    )
    result = client.post(
        "/api/v1/ask", headers=auth(credentials, 1), json={"question": QUESTION}
    ).json()
    assert result["outcome"] == "INSUFFICIENT_EVIDENCE" and result["answer"] is None
    assert len(provider.calls) == 0
