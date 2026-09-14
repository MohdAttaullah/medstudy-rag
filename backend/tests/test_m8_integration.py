"""M8 over the real PostgreSQL/Qdrant pipeline and the authorized API boundary.

Generator and verifier are deterministic doubles. What these tests establish is the ordering and
the boundary — that nothing is released without passing verification, that a repair happens at most
once, and that every failure path abstains — none of which needs a paid call to demonstrate.
"""

import os
from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.core.verification_config import ClaimVerificationConfig, RepairConfig
from app.generation.providers.fake import FakeProvider
from app.services.evidence import EvidenceService
from app.services.generation import GenerationService
from app.services.verification import VerificationService
from app.verification.verifier import FakeClaimVerifier, VerifierVerdict
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
from tests.test_m7_integration import answering, evidence_ids

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

QUERY = "synthetic table formula"
GOOD = "The source states the parameter values."


def responder(answer=GOOD, claim=None):
    """A generator that binds its statement to the first evidence block it was actually given."""

    def respond(question, rendered):
        first = evidence_ids(rendered)[0]
        return answering(
            {"answer": answer, "claims": [{"text": claim or answer, "evidence_ids": [first]}]}
        )

    return respond


@pytest.fixture
def pipeline(indexed, qdrant):  # noqa: F811 - pytest fixture imports
    build_sparse(indexed)
    client, control, credentials, body, _, _ = indexed
    retrieval = retrieval_service(control, StubQueryEncoder(control.settings.query_encoder), qdrant)
    control.evidence = EvidenceService(retrieval, control.settings, StubReranker())
    permissive = SufficiencyConfig(
        ordinary=EvidenceRequirement(),
        table=EvidenceRequirement(),
        formula=EvidenceRequirement(),
    )
    settings = control.settings.model_copy(
        update={
            "sufficiency": permissive,
            # The synthetic fixture text is not prose, so the semantic layer is exercised by its
            # own unit tests and the deterministic layer is what these tests drive end to end.
            "claim_verification": ClaimVerificationConfig(semantic_verification_enabled=True),
        }
    )
    provider = FakeProvider(responder())
    control.generation = GenerationService(control.evidence, settings, provider)
    verifier = FakeClaimVerifier()
    control.verification = VerificationService(control.generation, settings, verifier, provider)
    return client, control, credentials, body, provider, verifier


def post(client, credentials, **extra):
    return client.post(
        "/api/v1/retrieval/answer",
        headers=auth(credentials),
        json={"query": QUERY, **extra},
    )


def test_a_fully_supported_answer_is_released_as_verified(pipeline):
    client, _, credentials, body, _, verifier = pipeline
    result = post(client, credentials)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["verified"] is True
    # Releasing a verified answer is still not the user-facing Ask experience, which is M9.
    assert data["answering_enabled"] is False
    answer = data["verified_answer"]
    assert answer["verified"] is True and answer["verification_status"] == "VERIFIED"
    assert answer["repair_count"] == 0
    assert data["verification_abstention"] is None

    report = data["verification"]
    assert report["outcome"] == "PASS"
    assert report["material_claims"] == report["supported_claims"] >= 1
    assert report["verifier"]["verifier"] == "deterministic-fake"
    for key in ("claim_extraction_fingerprint", "final_policy_fingerprint"):
        assert report[key]
    supplied = {b["evidence_id"] for b in data["evidence_set"]["evidence_blocks"]}
    assert set(answer["cited_evidence_ids"]) <= supplied
    assert verifier.seen, "the semantic verifier must actually have been consulted"
    # The draft it verified is still typed as an unverified draft.
    assert data["draft"]["verification_status"] == "UNVERIFIED_AWAITING_CLAIM_VERIFICATION"


def test_an_unsupported_claim_abstains_and_releases_nothing(pipeline):
    client, control, credentials, _, _, _ = pipeline
    control.verification._verifier = FakeClaimVerifier(
        lambda claim: VerifierVerdict(verdict="UNSUPPORTED")
    )
    control.verification.settings = control.verification.settings.model_copy(
        update={"repair": RepairConfig(enabled=False)}
    )
    data = post(client, credentials).json()
    assert data["verified"] is False
    assert data["verified_answer"] is None
    abstention = data["verification_abstention"]
    assert abstention["reason"] == "UNSUPPORTED_CLAIM"
    assert abstention["verified"] is False
    assert "SEMANTICALLY_UNSUPPORTED" in abstention["reason_codes"]
    # The failed verdicts are retained rather than dropped so the refusal can be audited.
    assert any(v["verdict"] != "SUPPORTED" for v in data["verification"]["verifications"])


def test_one_repair_is_attempted_and_the_repaired_draft_is_fully_reverified(pipeline):
    client, control, credentials, _, provider, _ = pipeline
    attempts: list[int] = []

    def decide(claim):
        # Fail the first draft, accept the repaired one. The repaired draft still runs the whole
        # flow again; it is never released on the strength of having been repaired.
        attempts.append(1)
        return VerifierVerdict(verdict="SUPPORTED" if len(attempts) > 1 else "UNSUPPORTED")

    control.verification._verifier = FakeClaimVerifier(decide)
    data = post(client, credentials).json()
    assert data["verified"] is True
    assert data["verified_answer"]["repair_count"] == 1
    assert data["verification"]["repair_count"] == 1
    assert data["verification"]["durations_ms"]["repair_generation_ms"] > 0
    # The repair prompt reached the generator, carrying the failure it had to remove.
    assert len(provider.calls) == 2
    assert "failed verification" in provider.calls[1][1]
    assert "repair of a previous draft" in provider.calls[1][0]


def test_a_failed_repair_abstains_without_a_second_attempt(pipeline):
    client, control, credentials, _, provider, _ = pipeline
    control.verification._verifier = FakeClaimVerifier(
        lambda claim: VerifierVerdict(verdict="UNSUPPORTED")
    )
    data = post(client, credentials).json()
    assert data["verified"] is False
    assert data["verification"]["repair_count"] == 1
    assert data["verification_abstention"]["reason"] == "REPAIR_FAILED"
    # Exactly two generations: the original and the single repair.
    assert len(provider.calls) == 2


def test_a_repaired_draft_citing_unknown_evidence_still_fails(pipeline):
    client, control, credentials, _, _, _ = pipeline
    calls: list[int] = []

    def respond(question, rendered):
        calls.append(1)
        if len(calls) == 1:
            return responder()(question, rendered)
        return answering(
            {
                "answer": "Fabricated.",
                "claims": [{"text": "Fabricated.", "evidence_ids": [str(uuid4())]}],
            }
        )

    control.generation._provider = FakeProvider(respond)
    control.verification._provider = control.generation._provider
    control.verification._verifier = FakeClaimVerifier(
        lambda claim: VerifierVerdict(verdict="UNSUPPORTED")
    )
    # A repaired draft that invents a citation is a repair that failed, so the request abstains
    # rather than erroring — and the abstention names the citation, not a phantom verifier fault.
    result = post(client, credentials)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["verified"] is False and data["verified_answer"] is None
    assert data["verification_abstention"]["reason"] == "CITATION_INVALID"
    assert "UNKNOWN_CITATION" in data["verification_abstention"]["reason_codes"]


def test_verifier_failure_fails_closed(pipeline):
    client, control, credentials, _, _, _ = pipeline
    from app.verification.model import VerificationError

    control.verification._verifier = FakeClaimVerifier(
        lambda claim: VerificationError("VERIFIER_FAILED")
    )
    data = post(client, credentials).json()
    assert data["verified"] is False and data["verified_answer"] is None
    assert data["verification_abstention"]["reason"] == "VERIFICATION_UNAVAILABLE"


def test_an_insufficient_gate_never_reaches_generation_or_verification(pipeline):
    client, control, credentials, _, provider, verifier = pipeline
    demanding = EvidenceRequirement(min_independent_sources=9)
    control.generation.gate.config = SufficiencyConfig(
        ordinary=demanding, table=demanding, formula=demanding
    )
    data = post(client, credentials).json()
    assert data["sufficiency"]["status"] == "INSUFFICIENT"
    assert data["draft"] is None and data["verified"] is False
    assert data["verification"] is None
    assert not provider.calls and not verifier.seen


def test_unauthorized_and_reader_access_never_reach_the_pipeline(pipeline):
    client, _, credentials, _, provider, verifier = pipeline
    assert client.post("/api/v1/retrieval/answer", json={"query": QUERY}).status_code == 401
    assert (
        client.post(
            "/api/v1/retrieval/answer", headers=auth(credentials, 1), json={"query": QUERY}
        ).status_code
        == 403
    )
    assert not provider.calls and not verifier.seen


def test_a_client_cannot_submit_its_own_draft_evidence_or_verifier(pipeline):
    """Submitting a draft or evidence would be choosing what gets verified."""
    client, _, credentials, _, provider, _ = pipeline
    for field, value in (
        ("draft", {"answer": "Trusted."}),
        ("evidence_ids", [str(uuid4())]),
        ("verified", True),
        ("verifier", "openai"),
        ("tenant_id", str(uuid4())),
        ("verification", {"outcome": "PASS"}),
    ):
        assert post(client, credentials, **{field: value}).status_code == 422, field
    assert not provider.calls


def test_no_provider_or_verifier_key_appears_in_any_response(pipeline):
    client, _, credentials, _, _, _ = pipeline
    body = post(client, credentials).text
    for forbidden in ("api_key", "sk-", "x-api-key", "Authorization"):
        assert forbidden not in body
