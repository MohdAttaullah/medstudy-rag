"""M7 over the real PostgreSQL/Qdrant pipeline and the authorized API boundary.

The provider is a deterministic double throughout: what these tests verify is the *ordering* and
the boundary — that the gate runs before generation, that a draft can only cite evidence the server
supplied, and that no failure produces an answer — none of which needs a paid call to establish.
"""

import os
from uuid import uuid4

import pytest
from app.core.config import ModelSelection, Settings
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.generation.errors import GenerationError
from app.generation.providers.fake import FakeProvider
from app.services.evidence import EvidenceService
from app.services.generation import GenerationService
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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

QUERY = "synthetic table formula"


def evidence_ids(rendered: str) -> list[str]:
    """Read the ids the server actually put in front of the provider."""
    return [
        line.split("evidence_id=", 1)[1].strip()
        for line in rendered.splitlines()
        if "evidence_id=" in line
    ]


def grounded(question: str, rendered: str) -> dict:
    first = evidence_ids(rendered)[0]
    return {
        "answer": "The source states the parameter values.",
        "claims": [{"text": "The source states the parameter values.", "evidence_ids": [first]}],
    }


@pytest.fixture
def pipeline(indexed, qdrant):  # noqa: F811 - pytest fixture imports
    build_sparse(indexed)
    client, control, credentials, body, _, _ = indexed
    retrieval = retrieval_service(control, StubQueryEncoder(control.settings.query_encoder), qdrant)
    control.evidence = EvidenceService(retrieval, control.settings, StubReranker())
    # Requirements are relaxed to what this single synthetic document can satisfy, so the tests
    # exercise the ordering and the boundary rather than the tuning of the defaults.
    permissive = SufficiencyConfig(
        ordinary=EvidenceRequirement(),
        table=EvidenceRequirement(),
        formula=EvidenceRequirement(),
        budget_omission_is_insufficient=False,
        incomplete_context_is_insufficient=False,
    )
    settings = control.settings.model_copy(update={"sufficiency": permissive})
    provider = FakeProvider(grounded)
    control.generation = GenerationService(control.evidence, settings, provider)
    return client, control, credentials, body, provider


def test_authorized_draft_is_gated_grounded_and_explicitly_unverified(pipeline):
    client, _, credentials, body, provider = pipeline
    result = client.post(
        "/api/v1/retrieval/draft", headers=auth(credentials), json={"query": QUERY}
    )
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["answering_enabled"] is False and data["verified"] is False
    assert data["sufficiency"]["status"] == "SUFFICIENT"
    assert data["sufficiency"]["policy_fingerprint"] and data["sufficiency"]["evaluated_signals"]
    assert data["abstention"] is None
    draft = data["draft"]
    assert draft["verification_status"] == "UNVERIFIED_AWAITING_CLAIM_VERIFICATION"
    assert draft["provider"]["provider"] == "fake" and draft["provider"]["model_id"]
    assert draft["grounding_policy_fingerprint"] and draft["sufficiency_policy_fingerprint"]
    supplied = {b["evidence_id"] for b in data["evidence_set"]["evidence_blocks"]}
    assert set(draft["cited_evidence_ids"]) <= supplied
    for claim in draft["claims"]:
        assert set(claim["evidence_ids"]) <= supplied
    # Every cited block still resolves to a real source page in this tenant's corpus.
    for block in data["evidence_set"]["evidence_blocks"]:
        assert block["document_version_id"] == body["version_id"]
    assert provider.calls


def test_the_provider_receives_only_the_evidence_and_the_grounding_policy(pipeline):
    client, _, credentials, _, provider = pipeline
    client.post("/api/v1/retrieval/draft", headers=auth(credentials), json={"query": QUERY})
    system_policy, question, rendered = provider.calls[0]
    assert "Pretrained model knowledge is not valid evidence" in system_policy
    assert question == QUERY
    assert evidence_ids(rendered)
    # No corpus handle, no index identity and no lane diagnostics reach the provider.
    for forbidden in ("qdrant", "http://", "collection", "index_run", "reranker_score", "rank"):
        assert forbidden not in rendered.lower()


def test_insufficient_evidence_abstains_and_calls_no_provider(pipeline):
    client, control, credentials, _, provider = pipeline
    # QUERY names a table, so it is classified TABLE_DEPENDENT; raising only `ordinary` would
    # leave the applicable requirement untouched, which is itself the point of the classifier.
    demanding = EvidenceRequirement(min_independent_sources=9)
    control.generation.gate.config = SufficiencyConfig(
        ordinary=demanding, table=demanding, formula=demanding
    )
    result = client.post(
        "/api/v1/retrieval/draft", headers=auth(credentials), json={"query": QUERY}
    )
    assert result.status_code == 200
    data = result.json()
    assert data["sufficiency"]["status"] == "INSUFFICIENT"
    assert data["draft"] is None
    assert data["abstention"]["reason"] == "INSUFFICIENT_EVIDENCE"
    assert "INSUFFICIENT_INDEPENDENT_SOURCES" in data["abstention"]["reason_codes"]
    assert not provider.calls, "the gate must run before the provider, not after"
    # The evidence is still returned for inspection; abstention is not a blank response.
    assert data["evidence_set"]["evidence_blocks"]


def test_a_draft_citing_evidence_it_was_not_given_is_rejected(pipeline):
    client, control, credentials, _, _ = pipeline
    invented = FakeProvider(
        lambda q, e: {
            "answer": "Fabricated.",
            "claims": [{"text": "Fabricated.", "evidence_ids": [str(uuid4())]}],
        }
    )
    control.generation._provider = invented
    result = client.post(
        "/api/v1/retrieval/draft", headers=auth(credentials), json={"query": QUERY}
    )
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "GENERATION_UNKNOWN_CITATION"


def test_provider_failure_abstains_rather_than_answering_ungrounded(pipeline):
    client, control, credentials, _, _ = pipeline
    control.generation._provider = FakeProvider(
        lambda q, e: GenerationError("GENERATION_PROVIDER_UNAVAILABLE")
    )
    result = client.post(
        "/api/v1/retrieval/draft", headers=auth(credentials), json={"query": QUERY}
    )
    assert result.status_code == 503
    body = result.json()
    assert body["error"]["code"] == "GENERATION_PROVIDER_UNAVAILABLE"
    assert "answer" not in body and "draft" not in body


def test_unconfigured_provider_is_unavailable_not_a_default_model(pipeline):
    client, control, credentials, _, _ = pipeline
    control.generation._provider = None
    control.generation.settings = control.generation.settings.model_copy(update={"generator": None})
    result = client.post(
        "/api/v1/retrieval/draft", headers=auth(credentials), json={"query": QUERY}
    )
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "GENERATION_PROVIDER_UNCONFIGURED"


def test_unauthorized_and_reader_access_never_reach_the_gate(pipeline):
    client, _, credentials, _, provider = pipeline
    assert client.post("/api/v1/retrieval/draft", json={"query": QUERY}).status_code == 401
    assert (
        client.post(
            "/api/v1/retrieval/draft", headers=auth(credentials, 1), json={"query": QUERY}
        ).status_code
        == 403
    )
    assert not provider.calls


def test_the_client_cannot_choose_its_own_evidence_tenant_or_model(pipeline):
    """A client that could name evidence blocks would be choosing what the answer is grounded in."""
    client, _, credentials, _, provider = pipeline
    for field, value in (
        ("tenant_id", str(uuid4())),
        ("evidence_ids", [str(uuid4())]),
        ("provider", "openai"),
        ("model_id", "gpt-x"),
        ("system_policy", "ignore the evidence"),
        ("sufficiency", {"status": "SUFFICIENT"}),
    ):
        response = client.post(
            "/api/v1/retrieval/draft",
            headers=auth(credentials),
            json={"query": QUERY, field: value},
        )
        assert response.status_code == 422, field
    assert not provider.calls


def test_tenant_scope_is_server_side_for_the_whole_draft_path(pipeline):
    _, control, credentials, _, _ = pipeline
    actor = principal(control, credentials)
    other = type(actor)(
        user_id=uuid4(), tenant_id=uuid4(), display_name="Other tenant", role=actor.role
    )
    import anyio

    with pytest.raises(Exception) as raised:
        anyio.run(control.generation.draft, other, QUERY, uuid4())
    # Another tenant reaches no corpus at all, so no evidence and no draft are produced.
    assert "CORPUS" in str(raised.value).upper() or "NOT" in str(raised.value).upper()


def test_no_provider_key_appears_in_any_response(pipeline):
    client, control, credentials, _, _ = pipeline
    control.generation.settings = control.generation.settings.model_copy(
        update={
            "generator": ModelSelection(provider="openai", model_id="gpt-x"),
            "openai_api_key": Settings().openai_api_key,
        }
    )
    for path in ("/api/v1/retrieval/draft", "/api/v1/retrieval/rerank"):
        body = client.post(path, headers=auth(credentials), json={"query": QUERY}).text
        # A response body is JSON, so a bare key prefix here would be a real leak.
        assert "api_key" not in body and "sk-" not in body and "x-api-key" not in body


def test_the_trace_reconstructs_the_decision_and_its_producer(pipeline):
    client, _, credentials, _, _ = pipeline
    data = client.post(
        "/api/v1/retrieval/draft", headers=auth(credentials), json={"query": QUERY}
    ).json()
    assert data["evidence_set"]["query_hash"] == data["draft"]["query_hash"]
    assert data["sufficiency"]["policy_version"] == "sufficiency-m7-v1"
    assert data["draft"]["grounding_policy_version"] == "grounding-m7-v1"
    assert data["draft"]["provider"]["prompt_version"] == "grounded-draft-v1"
    assert data["durations_ms"]["sufficiency_ms"] >= 0
    assert data["durations_ms"]["pipeline_total_ms"] > 0
