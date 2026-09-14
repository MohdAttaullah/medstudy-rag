"""A provider may refuse. That is the whole of the new power it is given.

An out-of-corpus question used to end as `GENERATION_SCHEMA_VIOLATION`, and the cause was in the
contract rather than in the model: `ProviderDraft` required an answer *and* at least one claim
bound to evidence, so a provider that correctly concluded "this evidence is about something else"
had to either fabricate a claim or break the schema. It broke the schema, and a reader was told the
service had failed when what had actually happened was that the corpus did not cover the question.

The union makes declining representable. It is deliberately one-way: a provider can refuse, and
that is all. It cannot assert that evidence is relevant, cannot reopen the gate, cannot reach a
reader without M8, and cannot answer and decline at once — that shape does not exist. See ADR-022.
"""

from uuid import uuid4

import pytest
from app.generation.grounding.model import (
    AnswerDraft,
    DeclinedDraft,
    DraftClaim,
    ProviderResult,
)
from pydantic import ValidationError

CLAIM = {"text": "Warfarin is metabolised by CYP2C9.", "evidence_ids": [str(uuid4())]}


def answer_payload(**overrides) -> dict:
    return {
        "result": {
            "outcome": "ANSWER",
            "answer": "Warfarin is metabolised by CYP2C9.",
            "claims": [CLAIM],
            **overrides,
        }
    }


def declined_payload(**overrides) -> dict:
    return {
        "result": {
            "outcome": "DECLINED",
            "declination": "EVIDENCE_DOES_NOT_ADDRESS_QUESTION",
            **overrides,
        }
    }


# --------------------------------------------------------------- the two valid shapes


def test_an_answer_validates_and_carries_its_claims():
    result = ProviderResult.model_validate(answer_payload())
    assert isinstance(result.result, AnswerDraft)
    assert result.result.claims[0].text == CLAIM["text"]


def test_a_declination_validates_and_carries_no_answer_and_no_claims():
    result = ProviderResult.model_validate(declined_payload())
    assert isinstance(result.result, DeclinedDraft)
    assert result.result.declination == "EVIDENCE_DOES_NOT_ADDRESS_QUESTION"
    assert not hasattr(result.result, "answer")
    assert not hasattr(result.result, "claims")


def test_a_declination_may_record_an_explanation_for_the_decision_record():
    result = ProviderResult.model_validate(
        declined_payload(explanation="The evidence concerns bacterial culture, not cardiology.")
    )
    assert isinstance(result.result, DeclinedDraft)
    assert result.result.explanation


# --------------------------------------------- answering and declining are mutually exclusive


def test_a_declination_carrying_an_answer_is_rejected():
    with pytest.raises(ValidationError):
        ProviderResult.model_validate(declined_payload(answer="Here is an answer anyway."))


def test_a_declination_carrying_claims_is_rejected():
    with pytest.raises(ValidationError):
        ProviderResult.model_validate(declined_payload(claims=[CLAIM]))


def test_an_answer_carrying_a_declination_is_rejected():
    with pytest.raises(ValidationError):
        ProviderResult.model_validate(
            answer_payload(declination="EVIDENCE_DOES_NOT_ADDRESS_QUESTION")
        )


def test_the_two_branches_cannot_be_merged_into_one_object():
    """There is no representable value that both answers and declines."""
    with pytest.raises(ValidationError):
        ProviderResult.model_validate(
            {
                "result": {
                    "outcome": "DECLINED",
                    "declination": "EVIDENCE_DOES_NOT_ADDRESS_QUESTION",
                    "answer": "Both.",
                    "claims": [CLAIM],
                }
            }
        )


# ----------------------------------------------------------------- malformed stays malformed


@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"outcome": "MAYBE"}},
        {"result": {"outcome": "DECLINED"}},
        {"result": {"outcome": "DECLINED", "declination": "BECAUSE_I_SAID_SO"}},
        {"result": {"outcome": "ANSWER", "answer": "No claims."}},
        {"result": {"outcome": "ANSWER", "claims": [CLAIM]}},
        {"result": {"outcome": "ANSWER", "answer": "", "claims": [CLAIM]}},
        {"answer": "Flat, from before the union existed.", "claims": [CLAIM]},
        {"result": {}},
        {},
    ],
)
def test_anything_outside_the_union_is_a_schema_violation(payload):
    with pytest.raises(ValidationError):
        ProviderResult.model_validate(payload)


def test_an_unbound_claim_is_still_rejected():
    """The rule that every statement names its evidence is untouched by the union."""
    with pytest.raises(ValidationError):
        AnswerDraft(answer="Text.", claims=[DraftClaim(text="Text.", evidence_ids=[])])


def test_the_declination_reason_is_a_closed_vocabulary():
    """A provider cannot invent a new reason to refuse and have it recorded as declared."""
    assert DeclinedDraft.model_fields["declination"].annotation.__args__ == (
        "EVIDENCE_DOES_NOT_ADDRESS_QUESTION",
    )


def test_the_schema_offered_to_a_provider_is_a_single_top_level_object():
    """Both transports carry an object: an OpenAI json_schema and an Anthropic tool input."""
    schema = ProviderResult.model_json_schema()
    assert schema["type"] == "object"
    assert "result" in schema["properties"]
