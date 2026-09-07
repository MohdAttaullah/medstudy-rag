"""M8 API contracts.

`verified` is the one field in the system that may be true, and only when a `VerifiedAnswer` is
present. `answering_enabled` stays pinned false: releasing a verified answer object is not the same
as switching on the user-facing Ask experience, which is M9.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.evidence.model import EvidenceSet
from app.generation.grounding.model import Abstention, GroundedDraft
from app.schemas.reranking import RerankedCandidateView
from app.schemas.retrieval import SearchFilters, SearchResponse
from app.sufficiency.model import SufficiencyDecision
from app.verification.model import (
    VerificationAbstention,
    VerificationReport,
    VerifiedAnswer,
)


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["HYBRID_RRF"] = "HYBRID_RRF"
    filters: SearchFilters | None = None
    # Deliberately absent, as in M7: evidence ids, draft ids, tenant id, provider, model, prompt,
    # verifier and every policy override. A client that could submit its own draft or evidence
    # would be choosing what gets verified.


class AnswerResponse(BaseModel):
    correlation_id: UUID
    mode: Literal["VERIFIED_ANSWER"] = "VERIFIED_ANSWER"
    # The final Ask experience is M9. This endpoint is authorized inspection.
    answering_enabled: Literal[False] = False
    verified: bool = False
    first_stage: SearchResponse
    reranked: list[RerankedCandidateView]
    evidence_set: EvidenceSet
    sufficiency: SufficiencyDecision
    draft: GroundedDraft | None = None
    abstention: Abstention | None = None
    verification: VerificationReport | None = None
    verified_answer: VerifiedAnswer | None = None
    verification_abstention: VerificationAbstention | None = None
    durations_ms: dict[str, float]
