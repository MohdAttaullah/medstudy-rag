"""Provider-neutral reranker contract; scores are ranking diagnostics only."""

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import DomainError


class RerankingError(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(
            code,
            code.replace("_", " ").capitalize() + ".",
            503 if code.startswith("RERANKER") else 409,
        )


class RerankInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    chunk_id: UUID
    text: str = Field(min_length=1, max_length=100000)
    fused_rank: int = Field(ge=1)


class RerankResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    chunk_id: UUID
    reranker_score: float
    reranked_rank: int = 0
    input_hash: str
    token_count: int


class RerankerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_id: str
    model_revision: str
    tokenizer_revision: str
    files: dict[str, str]
    dtype: str
    device: str
    max_sequence_tokens: int
    representation: str
    score_semantics: str
    library_versions: dict[str, str]
    config_fingerprint: str


class Reranker(Protocol):
    @property
    def specification(self) -> RerankerSpec: ...
    def rerank(self, query: str, candidates: Sequence[RerankInput]) -> Sequence[RerankResult]: ...


def input_hash(query: str, text: str) -> str:
    return hashlib.sha256(
        json.dumps([query, text], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def ordered(
    candidates: Sequence[RerankInput], results: Sequence[RerankResult]
) -> list[RerankResult]:
    ranks = {item.chunk_id: item.fused_rank for item in candidates}
    if (
        len(ranks) != len(candidates)
        or len(results) != len(candidates)
        or {r.chunk_id for r in results} != set(ranks)
    ):
        raise RerankingError("RERANKER_INFERENCE_FAILED")
    if any(not math.isfinite(item.reranker_score) for item in results):
        raise RerankingError("RERANKER_NONFINITE_SCORE")
    return [
        r.model_copy(update={"reranked_rank": i})
        for i, r in enumerate(
            sorted(results, key=lambda r: (-r.reranker_score, ranks[r.chunk_id], str(r.chunk_id))),
            1,
        )
    ]
