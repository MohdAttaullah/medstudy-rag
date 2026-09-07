"""Explicit M6 API contracts; generation fields are absent."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.evidence.model import EvidenceSet
from app.schemas.retrieval import CandidateView, SearchFilters, SearchResponse


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["HYBRID_RRF"] = "HYBRID_RRF"
    filters: SearchFilters | None = None


class RerankedCandidateView(CandidateView):
    reranker_score: float
    reranked_rank: int
    input_hash: str
    token_count: int
    selected_anchor: bool


class RerankResponse(BaseModel):
    correlation_id: UUID
    mode: Literal["RERANKED"] = "RERANKED"
    answering_enabled: Literal[False] = False
    first_stage: SearchResponse
    reranked: list[RerankedCandidateView]
    evidence_set: EvidenceSet
