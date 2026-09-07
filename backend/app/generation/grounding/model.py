"""Grounded-draft contracts.

A draft is not an answer. It is the model's attempt to state, using only the supplied EvidenceSet,
what that evidence says — with every statement bound to the evidence blocks it came from. M7
validates that those bindings exist and are real. Whether each statement is actually *entailed* by
the evidence it cites is M8's claim verification and is not asserted here.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DraftClaim(BaseModel):
    """One atomic statement together with the evidence the provider bound it to."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=20)


class ProviderDraft(BaseModel):
    """Exactly what a provider is allowed to return. Anything else is a schema violation."""

    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1, max_length=32000)
    claims: list[DraftClaim] = Field(min_length=1, max_length=200)
    # The provider's own report that the evidence did not cover the question. It is a signal for
    # review, never a licence to fill the gap from pretrained knowledge.
    evidence_gap: str | None = Field(default=None, max_length=2000)


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["openai", "anthropic", "fake"]
    model_id: str
    endpoint: str
    temperature: float
    max_output_tokens: int
    schema_version: str
    prompt_version: str


class GroundedDraft(BaseModel):
    """The M7 output. Explicitly unverified."""

    model_config = ConfigDict(extra="forbid")
    draft_id: UUID
    # Both pinned by type so no caller can present a draft as a finished answer.
    status: Literal["GROUNDED_DRAFT"] = "GROUNDED_DRAFT"
    verification_status: Literal["UNVERIFIED_AWAITING_CLAIM_VERIFICATION"] = (
        "UNVERIFIED_AWAITING_CLAIM_VERIFICATION"
    )
    answer: str
    claims: list[DraftClaim]
    cited_evidence_ids: list[UUID]
    uncited_evidence_ids: list[UUID]
    evidence_gap: str | None
    query_hash: str
    provider: ProviderSpec
    grounding_policy_version: str
    grounding_policy_fingerprint: str
    sufficiency_policy_fingerprint: str
    durations_ms: dict[str, float]


class Abstention(BaseModel):
    """Returned instead of a draft. Abstention is a successful outcome, not an error path."""

    model_config = ConfigDict(extra="forbid")
    abstained: Literal[True] = True
    reason: Literal["INSUFFICIENT_EVIDENCE", "CONFLICTING_EVIDENCE", "GENERATION_FAILED"]
    reason_codes: list[str]
    message: str
    conflicting_evidence_ids: list[UUID] = Field(default_factory=list)
