"""HTTP adapter for the private, warm retrieval runtime."""

from collections.abc import Sequence

import httpx

from app.core.reranking_config import MODEL_FILES, RerankerConfig
from app.reranking.model import (
    Reranker,
    RerankerSpec,
    RerankingError,
    RerankInput,
    RerankResult,
    input_hash,
    ordered,
)


class HttpReranker:
    def __init__(self, config: RerankerConfig) -> None:
        self.config = config

    def _call(
        self, query: str, items: Sequence[RerankInput]
    ) -> tuple[RerankerSpec, list[RerankResult]]:
        try:
            response = httpx.post(
                self.config.endpoint.rstrip("/") + "/rerank",
                json={"query": query, "candidates": [i.model_dump(mode="json") for i in items]},
                timeout=self.config.request_timeout_seconds,
            )
            if response.status_code != 200:
                code = response.json().get("error", {}).get("code")
                if code not in {
                    "RERANKER_UNAVAILABLE",
                    "RERANKER_MODEL_MISMATCH",
                    "RERANKER_INPUT_TOO_LONG",
                    "RERANKER_INFERENCE_FAILED",
                    "RERANKER_NONFINITE_SCORE",
                    "RERANKER_TIMEOUT",
                }:
                    code = "RERANKER_UNAVAILABLE"
                raise RerankingError(code)
            data = response.json()
            spec = RerankerSpec.model_validate(data["specification"])
            verify_spec(spec, self.config)
            results = [RerankResult.model_validate(r) for r in data["results"]]
            texts = {i.chunk_id: i.text for i in items}
            if any(
                r.chunk_id not in texts
                or r.input_hash != input_hash(query, texts[r.chunk_id])
                or not 0 < r.token_count <= 512
                for r in results
            ):
                raise RerankingError("RERANKER_INFERENCE_FAILED")
            return spec, ordered(items, results)
        except httpx.TimeoutException:
            raise RerankingError("RERANKER_TIMEOUT") from None
        except RerankingError:
            raise
        except Exception:
            raise RerankingError("RERANKER_UNAVAILABLE") from None

    @property
    def specification(self) -> RerankerSpec:
        return self._call("", ())[0]

    def rerank(self, query: str, candidates: Sequence[RerankInput]) -> Sequence[RerankResult]:
        return self._call(query, candidates)[1]


def verify_spec(spec: RerankerSpec, config: RerankerConfig) -> None:
    if (
        any(
            getattr(spec, k) != getattr(config, k)
            for k in (
                "model_id",
                "model_revision",
                "tokenizer_revision",
                "dtype",
                "device",
                "max_sequence_tokens",
                "representation",
                "score_semantics",
            )
        )
        or spec.files != MODEL_FILES
    ):
        raise RerankingError("RERANKER_MODEL_MISMATCH")


def build_reranker(config: RerankerConfig) -> Reranker:
    if config.endpoint:
        return HttpReranker(config)
    from app.reranking.medcpt import MedCPTReranker

    return MedCPTReranker(config)
