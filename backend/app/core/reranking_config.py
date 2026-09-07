"""M6 query-side policies. No stored embeddings or ingestion state changes."""

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.retrieval_config import _fingerprint

MODEL_REVISION = "71caf65d4927987813984f54c284405a13fcca49"
MODEL_FILES = {
    "added_tokens.json": "691a5ce0135045c12b8410af8d472ff8de864094df40ac9af418d6c644c7588d",
    "config.json": "1f375c7e8081f043c99fdb9c80059c44ed757cce7b05d333b7f3c81b1b713a42",
    "pytorch_model.bin": "61d5ccd48869e03500544525fc231641d7daa9ba267b202c82724750038dc1e0",
    "special_tokens_map.json": "cb63d0cbbf45160dc9cd786a257759593b798ae0c72957011016dbc3972df4e4",
    "tokenizer.json": "6e046044df8a2fcedb10607075dca187cae61d806c0d80a96c5b81017edc90c9",
    "tokenizer_config.json": "25e63abb25f637aa24981117b13394370693dd7bc140903d95008599b3abc1a9",
    "vocab.txt": "79489a52be45e6fa033521e8ce8e4f62aedc0a742ee2aa6fc04667e5b0b1454d",
}


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class RerankerConfig(Policy):
    version: Literal["medcpt-cross-m6-v1"] = "medcpt-cross-m6-v1"
    model_id: Literal["ncbi/MedCPT-Cross-Encoder"] = "ncbi/MedCPT-Cross-Encoder"
    model_revision: Literal["71caf65d4927987813984f54c284405a13fcca49"] = (
        "71caf65d4927987813984f54c284405a13fcca49"
    )
    tokenizer_revision: Literal["71caf65d4927987813984f54c284405a13fcca49"] = (
        "71caf65d4927987813984f54c284405a13fcca49"
    )
    max_sequence_tokens: Literal[512] = 512
    representation: Literal["normalized-query+full-m3-retrieval-text-v1"] = (
        "normalized-query+full-m3-retrieval-text-v1"
    )
    score_semantics: Literal["RAW_LOGIT"] = "RAW_LOGIT"
    truncation_policy: Literal["REJECT"] = "REJECT"
    dtype: Literal["float32"] = "float32"
    device: Literal["cpu"] = "cpu"
    batch_size: int = Field(default=8, ge=1, le=40)
    torch_threads: int = Field(default=4, ge=1, le=16)
    model_cache_dir: Path = Path(".local/models/reranking")
    offline: Literal[True] = True
    endpoint: str = ""
    request_timeout_seconds: float = Field(default=90, gt=0, le=300)


class RerankingConfig(Policy):
    version: Literal["reranking-m6-v1"] = "reranking-m6-v1"
    candidate_top_k: int = Field(default=20, ge=1, le=40)
    final_top_k: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.final_top_k > self.candidate_top_k:
            raise ValueError("Anchor count exceeds candidate pool")
        return self


class ExpansionConfig(Policy):
    version: Literal["expansion-m6-v1"] = "expansion-m6-v1"
    parent_enabled: bool = True
    max_parent_tokens: int = Field(default=512, ge=1, le=2048)
    max_expansions_per_anchor: int = Field(default=3, ge=0, le=5)
    previous_siblings: int = Field(default=1, ge=0, le=1)
    next_siblings: int = Field(default=1, ge=0, le=1)
    max_neighbour_tokens: int = Field(default=256, ge=1, le=1024)


class EvidenceBudgetConfig(Policy):
    version: Literal["evidence-budget-m6-v1"] = "evidence-budget-m6-v1"
    max_total_tokens: int = Field(default=4096, ge=1, le=16384)
    max_blocks: int = Field(default=20, ge=1, le=100)
    max_tokens_per_block: int = Field(default=1024, ge=1, le=4096)
