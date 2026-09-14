"""Figure classification over the real pipeline, and the claim rule that stands behind it.

The unit tests pin `classify` and `check_structured_evidence` in isolation. These pin the thing a
reader actually experiences: a question that asks about a picture is refused before a provider is
called, a factual question is no longer refused because a caption ranked beside its answer, and a
released answer never carries a figure citation — because if the model cites one, M8 refuses it.

See ADR-023.
"""

import os
import re
from uuid import uuid4

import pytest
from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.generation.providers.fake import FakeProvider
from app.services.ask import AskService
from app.services.evidence import EvidenceService
from app.services.generation import GenerationService
from app.services.verification import VerificationService
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
from tests.test_m7_integration import answering

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

#: No visual cue. The fixture corpus returns a FIGURE_CONTEXT anchor for it alongside prose.
FACTUAL = "stub caption"
#: A cue the reader supplied themselves.
VISUAL = "What does the picture show?"

BLOCK = re.compile(r"evidence_id=(\S+)\n.*?\n\s*type: (\w+);", re.DOTALL)


def supplied(rendered: str) -> list[tuple[str, str]]:
    """The (evidence_id, chunk_type) pairs the server actually put in front of the provider."""
    return BLOCK.findall(rendered)


def cite(rendered: str, chunk_type: str | None = None, exclude: str | None = None) -> dict:
    """A draft citing the first block of a chosen kind, or the first block that is not one."""
    for evidence_id, kind in supplied(rendered):
        if chunk_type is not None and kind != chunk_type:
            continue
        if exclude is not None and kind == exclude:
            continue
        return answering(
            {
                "answer": "The source states the parameter values.",
                "claims": [
                    {
                        "text": "The source states the parameter values.",
                        "evidence_ids": [evidence_id],
                    }
                ],
            }
        )
    raise AssertionError(f"no block matched chunk_type={chunk_type} exclude={exclude}")


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
    provider = FakeProvider(lambda q, e: cite(e, exclude="FIGURE_CONTEXT"))
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


# ------------------------------------------------------------------- the evidence really is mixed


def test_the_factual_question_really_does_retrieve_a_figure_anchor(stack):
    """Everything below is meaningless if the figure never reaches the EvidenceSet."""
    client, _, credentials, _, _ = stack
    data = client.post(
        "/api/v1/retrieval/draft", headers=auth(credentials), json={"query": FACTUAL}
    ).json()
    kinds = [b["chunk_type"] for b in data["evidence_set"]["evidence_blocks"]]
    assert "FIGURE_CONTEXT" in kinds
    assert [k for k in kinds if k != "FIGURE_CONTEXT"], "and readable prose beside it"
    assert data["sufficiency"]["question_kind"] != "FIGURE_DEPENDENT"


# --------------------------------------------------------------- a real visual question abstains


def test_a_question_asking_about_a_picture_abstains_before_generation(stack):
    client, _, credentials, provider, verifier = stack
    data = ask(client, credentials, VISUAL).json()

    assert data["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "VISUAL_INTERPRETATION_UNAVAILABLE" in data["reason_codes"]
    assert data["answer"] is None and data["citations"] == []
    # Vision is not enabled and no provider is asked to pretend otherwise.
    assert provider.calls == []
    assert verifier.seen == []


# --------------------------------------------------- a factual question is no longer poisoned


def test_a_factual_question_with_a_figure_anchor_now_reaches_generation(stack):
    client, _, credentials, provider, _ = stack
    data = ask(client, credentials, FACTUAL).json()
    assert "VISUAL_INTERPRETATION_UNAVAILABLE" not in data["reason_codes"]
    assert provider.calls, "the caption beside the answer no longer refuses the question"


def test_an_answer_grounded_only_in_text_can_be_released(stack):
    client, _, credentials, _, verifier = stack
    data = ask(client, credentials, FACTUAL).json()
    assert data["outcome"] == "VERIFIED"
    assert data["answer"]
    assert verifier.seen, "M8 still ran"


def test_a_released_answer_never_cites_a_figure(stack):
    client, _, credentials, _, _ = stack
    data = ask(client, credentials, FACTUAL).json()
    assert data["outcome"] == "VERIFIED"
    assert data["citations"]
    assert all(c["chunk_type"] != "FIGURE_CONTEXT" for c in data["citations"])
    assert all(a["kind"] != "FIGURE" for c in data["citations"] for a in c["artifacts"]), (
        "no released citation may carry a figure artifact"
    )


# ------------------------------------------------------- and M8 still refuses a figure citation


def test_a_draft_citing_the_figure_is_refused_by_claim_verification(stack):
    """The safety property the classifier change relies on, exercised end to end."""
    client, control, credentials, _, _ = stack
    figure_citing = FakeProvider(lambda q, e: cite(e, chunk_type="FIGURE_CONTEXT"))
    control.generation._provider = figure_citing
    control.verification._provider = figure_citing

    data = ask(client, credentials, FACTUAL).json()
    assert data["outcome"] != "VERIFIED"
    assert data["answer"] is None
    assert data["citations"] == []
    assert "VISUAL_INTERPRETATION_REQUIRED" in str(data["reason_codes"])


def test_the_figure_block_is_still_offered_to_the_provider_as_caption_text(stack):
    """Captions remain readable context. They simply cannot support a released claim."""
    client, _, credentials, provider, _ = stack
    ask(client, credentials, FACTUAL)
    _, _, rendered = provider.calls[0]
    assert ("FIGURE_CONTEXT") in rendered
    assert "original figure not interpreted; caption text only" in rendered
