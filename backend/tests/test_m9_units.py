"""M9 safety contracts: the public Ask response cannot carry an unverified answer.

The failure this milestone must make impossible is an unverified draft rendered to a reader as an
answer. Most of what follows is an attempt to construct exactly that, and an assertion that the
contract refuses.
"""

import re
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.ask_config import AskConfig
from app.schemas.ask import (
    AskCitation,
    AskClaim,
    AskRequest,
    AskResponse,
    AskSource,
    CitationSpan,
    ConversationTurnView,
)
from app.security.auth import PERMISSIONS, ROLE_PERMISSIONS
from app.services.ask import MESSAGES, PROVIDER_FAILURES, AskService
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]


def citation(**overrides):
    element = uuid4()
    base = dict(
        citation_id=uuid4(),
        ordinal=1,
        document_id=uuid4(),
        document_version_id=uuid4(),
        parse_run_id=uuid4(),
        chunk_run_id=uuid4(),
        document_title="Synthetic reference",
        source_type="TEXTBOOK",
        authority_level="REFERENCE",
        chunk_type="TEXT_CHILD",
        pages=[3],
        spans=[
            CitationSpan(
                element_id=element, page=3, start=0, end=20, role="PRIMARY", bbox=(1, 2, 3, 4)
            )
        ],
        artifacts=[],
        cited_text="A synthetic source passage.",
    )
    return AskCitation(**{**base, **overrides})


def response(**overrides):
    base = dict(
        correlation_id=uuid4(),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        question="What does the source state?",
        outcome="VERIFIED",
        verified=True,
        answer="The source states the association.",
        claims=[],
        citations=[],
        sources=[],
        message=MESSAGES["VERIFIED"],
        reason_codes=[],
        stages=[],
        created_at="2026-09-07T00:00:00+00:00",
    )
    return AskResponse(**{**base, **overrides})


# --- One outcome vocabulary, four places ---------------------------------------------------------


def test_every_outcome_has_a_message_and_a_storable_value():
    """The vocabulary spans a Literal, a message map and a database CHECK.

    They are edited in different files, so an outcome added to one and missed in another fails at
    request time -- a KeyError rendering the response, or an integrity error writing the turn.
    """
    from typing import get_args

    from app.models.conversations import OUTCOMES
    from app.schemas.ask import AskOutcome

    declared = set(get_args(AskOutcome))
    assert declared == set(MESSAGES), "every outcome needs a reader-facing message"
    assert declared == set(OUTCOMES), "every outcome must satisfy the conversation_turns CHECK"


def test_the_out_of_scope_message_refuses_without_giving_clinical_direction():
    message = MESSAGES["OUT_OF_SCOPE"].lower()
    assert "educational" in message and "clinician" in message
    # It may send an urgent reader to care; it may not do the system's forbidden work.
    for forbidden in ("you should take", "mg", "diagnos", "prescrib", "your dose"):
        assert forbidden not in message


# --- The display rule is unrepresentable to violate ----------------------------------------------


def test_a_verified_outcome_carries_an_answer():
    assert response().answer and response().verified is True


@pytest.mark.parametrize(
    "outcome",
    ["INSUFFICIENT_EVIDENCE", "CONFLICTING_EVIDENCE", "UNVERIFIED", "FAILED"],
)
def test_no_non_verified_outcome_may_carry_an_answer(outcome):
    """The exact contract shape that would let an interface render a rejected draft."""
    with pytest.raises(ValidationError):
        response(outcome=outcome, verified=False, answer="Take 50 mg twice daily.")


@pytest.mark.parametrize(
    "outcome",
    ["INSUFFICIENT_EVIDENCE", "CONFLICTING_EVIDENCE", "UNVERIFIED", "FAILED"],
)
def test_every_refusal_states_which_kind_it_was(outcome):
    built = response(
        outcome=outcome, verified=False, answer=None, message=MESSAGES[outcome], reason_codes=["X"]
    )
    assert built.answer is None and built.verified is False
    # Four distinct messages, so a reader can tell missing evidence from a broken provider.
    assert len({MESSAGES[key] for key in MESSAGES}) == len(MESSAGES)


def test_a_verified_outcome_without_an_answer_is_refused():
    with pytest.raises(ValidationError):
        response(answer=None)


def test_verified_must_agree_with_the_outcome():
    with pytest.raises(ValidationError):
        response(verified=False)
    with pytest.raises(ValidationError):
        response(outcome="FAILED", verified=True, answer=None, message=MESSAGES["FAILED"])


def test_a_refusal_carries_no_citations_or_sources():
    """Citations beside a refusal would imply the refusal rested on them."""
    with pytest.raises(ValidationError):
        response(
            outcome="INSUFFICIENT_EVIDENCE",
            verified=False,
            answer=None,
            message=MESSAGES["INSUFFICIENT_EVIDENCE"],
            citations=[citation()],
        )


def test_a_stored_turn_obeys_the_same_rule_on_the_way_out():
    with pytest.raises(ValidationError):
        ConversationTurnView(
            turn_id=uuid4(),
            sequence_number=1,
            question="q",
            outcome="UNVERIFIED",
            verified=False,
            answer="A draft that failed verification.",
            message=MESSAGES["UNVERIFIED"],
            reason_codes=[],
            citations=[],
            sources=[],
            created_at="2026-09-07T00:00:00+00:00",
        )


def test_the_response_exposes_no_draft_or_provider_internals():
    fields = set(AskResponse.model_fields)
    for forbidden in (
        "draft",
        "draft_answer",
        "sufficiency",
        "verification",
        "evidence_set",
        "first_stage",
        "reranked",
        "prompt",
        "provider",
        "rationale",
    ):
        assert forbidden not in fields


def test_no_confidence_number_exists_anywhere_in_the_contract():
    dumped = response().model_dump_json()
    for forbidden in ("confidence", "probability", "score", "accuracy"):
        assert forbidden not in dumped.lower()


# --- The request boundary -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "evidence_ids",
        "verified",
        "outcome",
        "answer",
        "provider",
        "model_id",
        "system_policy",
        "sufficiency",
        "verification",
        "authority_level",
    ],
)
def test_a_client_cannot_assert_any_part_of_the_answer_decision(field):
    with pytest.raises(ValidationError):
        AskRequest.model_validate({"question": "A question.", field: "anything"})


def test_a_question_and_permitted_filters_are_accepted():
    request = AskRequest.model_validate(
        {"question": "A question.", "filters": {"document_ids": [str(uuid4())]}}
    )
    assert request.question == "A question." and request.filters is not None


# --- Outcome derivation reads the pipeline; it re-decides nothing ---------------------------------


def test_a_verified_payload_becomes_a_verified_outcome():
    payload = {
        "verified": True,
        "verified_answer": {"answer": "x"},
        "sufficiency": {"status": "SUFFICIENT"},
    }
    assert AskService._outcome(payload) == "VERIFIED"


def test_a_conflicting_gate_is_reported_as_a_conflict_not_as_missing_evidence():
    payload = {"verified": False, "verified_answer": None, "sufficiency": {"status": "CONFLICTING"}}
    assert AskService._outcome(payload) == "CONFLICTING_EVIDENCE"


def test_an_insufficient_gate_is_reported_as_missing_evidence():
    payload = {
        "verified": False,
        "verified_answer": None,
        "sufficiency": {"status": "INSUFFICIENT"},
    }
    assert AskService._outcome(payload) == "INSUFFICIENT_EVIDENCE"


def test_a_sufficient_gate_whose_draft_failed_verification_is_reported_as_unverified():
    """Distinct from missing evidence: evidence existed and the draft did not survive it."""
    payload = {"verified": False, "verified_answer": None, "sufficiency": {"status": "SUFFICIENT"}}
    assert AskService._outcome(payload) == "UNVERIFIED"


def test_provider_failures_are_never_mislabelled_as_missing_evidence():
    for code in ("GENERATION_PROVIDER_TIMEOUT", "VERIFIER_FAILED", "GENERATION_RATE_LIMITED"):
        assert code in PROVIDER_FAILURES
    assert "The indexed sources do not support" in MESSAGES["INSUFFICIENT_EVIDENCE"]
    assert "technical failure, not a" in MESSAGES["FAILED"]


# --- Sources are grouped from citations, never from retrieval candidates --------------------------


def test_sources_group_citations_by_document_version():
    version, document = uuid4(), uuid4()
    first = citation(document_id=document, document_version_id=version, pages=[3])
    second = citation(document_id=document, document_version_id=version, pages=[4], ordinal=2)
    other = citation(pages=[9], ordinal=3)
    sources = AskService._sources([first, second, other])
    assert len(sources) == 2
    combined = next(s for s in sources if s.document_version_id == version)
    assert combined.pages == [3, 4]
    assert combined.citation_ids == [first.citation_id, second.citation_id]


def test_a_source_list_is_built_only_from_citations():
    assert AskService._sources([]) == []
    assert isinstance(AskService._sources([citation()])[0], AskSource)


# --- Geometry is real or absent -------------------------------------------------------------------


def test_a_missing_bounding_box_is_reported_as_absent_rather_than_invented():
    span = CitationSpan(element_id=uuid4(), page=1, start=0, end=5, role="PRIMARY", bbox=None)
    assert span.bbox is None
    with_box = CitationSpan(
        element_id=uuid4(), page=1, start=0, end=5, role="PRIMARY", bbox=(1.0, 2.0, 3.0, 4.0)
    )
    assert with_box.bbox == (1.0, 2.0, 3.0, 4.0)


def test_authority_stays_on_every_citation():
    """Assessment material must never be renderable as if it were a reference source."""
    assessment = citation(source_type="ANSWER_KEY", authority_level="ASSESSMENT")
    assert assessment.authority_level == "ASSESSMENT"
    assert "authority_level" in AskCitation.model_fields
    assert "authority_level" in AskSource.model_fields


# --- Policy and permissions -----------------------------------------------------------------------


def test_the_ask_policy_pins_the_safety_rules_by_type():
    config = AskConfig()
    assert config.requires_verified_pass is True
    assert config.stream_answer_tokens is False
    assert config.persist_unverified_drafts is False


def test_no_configuration_can_relax_the_verified_requirement():
    """A boolean that could switch this off would be where the safety chain becomes optional."""
    with pytest.raises(ValidationError):
        AskConfig(requires_verified_pass=False)
    with pytest.raises(ValidationError):
        AskConfig(stream_answer_tokens=True)
    with pytest.raises(ValidationError):
        AskConfig(persist_unverified_drafts=True)


def test_asking_is_separate_from_inspecting():
    """A reader may ask; only a curator or admin sees drafts, lane scores and verdicts."""
    reader = ROLE_PERMISSIONS["reader"]
    assert "ask:submit" in reader and "conversation:read" in reader
    for inspection in ("retrieval:search", "generation:draft", "generation:verify"):
        assert inspection not in reader
        assert inspection in PERMISSIONS


def test_earlier_milestone_responses_still_refuse_to_answer():
    """M5–M8 contracts keep `answering_enabled` false; none became an answering endpoint."""
    from app.schemas.generation import DraftResponse
    from app.schemas.reranking import RerankResponse
    from app.schemas.retrieval import SearchResponse
    from app.schemas.verification import AnswerResponse

    for schema in (SearchResponse, RerankResponse, DraftResponse, AnswerResponse):
        assert schema.model_fields["answering_enabled"].annotation.__args__ == (False,)
    # Only the public Ask contract may say otherwise, and it says so by type.
    assert AskResponse.model_fields["answering_enabled"].annotation.__args__ == (True,)


def test_no_provider_key_or_prompt_reaches_the_frontend():
    key = re.compile(r"sk-[A-Za-z0-9_-]{16,}")
    for path in Path(ROOT, "frontend/src").rglob("*.ts*"):
        body = path.read_text(encoding="utf-8")
        assert "VITE_OPENAI" not in body and "VITE_ANTHROPIC" not in body
        assert not key.search(body), path
        # The browser must not carry a prompt or a model identity of its own.
        assert "system_policy" not in body
        assert "api_key" not in body


def test_the_ui_never_decides_the_outcome_itself():
    """Rendering the server's outcome is the frontend's job; computing it is not.

    Naming `INSUFFICIENT_EVIDENCE` to render it is expected. Reading a lane score, a sufficiency
    signal, a verification verdict or a draft would mean the browser was re-deciding what the
    pipeline already decided — and could decide differently.
    """
    for name in ("Ask.tsx", "Answer.tsx"):
        source = Path(ROOT, "frontend/src/features/ask", name).read_text(encoding="utf-8")
        # Comments explain what the page deliberately does not do, so they name the very things
        # being checked for. Only executable code is scanned.
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        source = re.sub(r"^\s*//.*$", "", source, flags=re.M)
        for forbidden in (
            "reranker_score",
            "fused_score",
            "dense_score",
            "sparse_score",
            "evaluated_signals",
            "verifications",
            "evidence_set",
            "draft",
        ):
            assert forbidden not in source, (name, forbidden)


def test_claims_reference_citations_by_identity():
    claim = AskClaim(text="A statement.", citation_ids=[uuid4()])
    assert claim.citation_ids
