"""Curator parse-review wire types."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.documents import ORMView


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: One value today. Recording a rejection is a separate decision with its own downstream
    #: meaning and is deliberately not implemented here.
    decision: Literal["ACCEPT"]
    #: Required, and required to say something. This is the only durable explanation of why a
    #: flagged parse was admitted, and it has to still mean something to a different reader
    #: months later.
    rationale: str = Field(min_length=10, max_length=2000)

    @field_validator("rationale")
    @classmethod
    def rationale_is_substantive(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 10:
            raise ValueError("rationale must explain the decision")
        return stripped


class ReviewDecisionView(ORMView):
    id: UUID
    parse_run_id: UUID
    decision: str
    reviewer_user_id: UUID
    rationale: str
    validation_result_at_decision: str
    finding_count: int
    findings_digest: str
    configuration_fingerprint: str
    correlation_id: UUID
    created_at: datetime
