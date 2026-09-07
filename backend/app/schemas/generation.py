"""M7 API contracts.

`answering_enabled` and `verified` are both pinned false by type: the response can carry a grounded
draft, but no shape of this contract can present one as a delivered, verified answer.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.evidence.model import EvidenceSet
from app.generation.grounding.model import Abstention, GroundedDraft
from app.schemas.reranking import RerankedCandidateView
from app.schemas.retrieval import SearchFilters, SearchResponse
from app.sufficiency.model import SufficiencyDecision


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["HYBRID_RRF"] = "HYBRID_RRF"
    filters: SearchFilters | None = None
    # Deliberately absent: evidence ids, tenant id, provider, model, prompt, schema and any policy
    # override. The server decides all of them; a client that could name its own evidence blocks
    # would be choosing what the answer is grounded in.


class DraftResponse(BaseModel):
    correlation_id: UUID
    mode: Literal["GROUNDED_DRAFT"] = "GROUNDED_DRAFT"
    answering_enabled: Literal[False] = False
    verified: Literal[False] = False
    first_stage: SearchResponse
    reranked: list[RerankedCandidateView]
    evidence_set: EvidenceSet
    sufficiency: SufficiencyDecision
    draft: GroundedDraft | None = None
    abstention: Abstention | None = None
    durations_ms: dict[str, float]
