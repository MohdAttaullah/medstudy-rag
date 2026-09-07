"""Isolated, offline, pinned MedCPT sequence-classification adapter."""

import hashlib
from collections.abc import Sequence
from importlib.metadata import version
from threading import Lock
from typing import Any

from app.core.reranking_config import MODEL_FILES, RerankerConfig
from app.reranking.model import (
    RerankerSpec,
    RerankingError,
    RerankInput,
    RerankResult,
    input_hash,
    ordered,
)


class MedCPTReranker:
    def __init__(self, config: RerankerConfig) -> None:
        self.config = config
        self._model: Any = None
        self._tokenizer: Any = None
        self._lock = Lock()

    def load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from huggingface_hub import snapshot_download

                path = snapshot_download(
                    self.config.model_id,
                    revision=self.config.model_revision,
                    cache_dir=str(self.config.model_cache_dir),
                    local_files_only=True,
                    allow_patterns=list(MODEL_FILES),
                )
            except Exception:
                raise RerankingError("RERANKER_UNAVAILABLE") from None
            from pathlib import Path

            try:
                for name, checksum in MODEL_FILES.items():
                    with (Path(path) / name).open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                            raise ValueError("Checksum")
            except Exception:
                raise RerankingError("RERANKER_MODEL_MISMATCH") from None
            try:
                import torch
                from transformers import AutoModelForSequenceClassification, AutoTokenizer

                torch.set_num_threads(self.config.torch_threads)
                tokenizer = AutoTokenizer.from_pretrained(
                    path, local_files_only=True, trust_remote_code=False
                )
                model = AutoModelForSequenceClassification.from_pretrained(
                    path,
                    local_files_only=True,
                    trust_remote_code=False,
                    use_safetensors=False,
                    weights_only=True,
                    dtype=torch.float32,
                )
                if model.config.num_labels != 1 or model.config.max_position_embeddings != 512:
                    raise ValueError("Model semantics")
                model.to("cpu").eval()
                self._tokenizer, self._model = tokenizer, model
            except Exception:
                raise RerankingError("RERANKER_UNAVAILABLE") from None

    @property
    def specification(self) -> RerankerSpec:
        self.load()
        c = self.config
        return RerankerSpec(
            model_id=c.model_id,
            model_revision=c.model_revision,
            tokenizer_revision=c.tokenizer_revision,
            files=MODEL_FILES,
            dtype=c.dtype,
            device=c.device,
            max_sequence_tokens=c.max_sequence_tokens,
            representation=c.representation,
            score_semantics=c.score_semantics,
            library_versions={
                n: version(n) for n in ("torch", "transformers", "tokenizers", "numpy")
            },
            config_fingerprint=c.fingerprint,
        )

    def rerank(self, query: str, candidates: Sequence[RerankInput]) -> Sequence[RerankResult]:
        self.load()
        if not candidates:
            return ()
        if not query.strip() or len(query) > 2000 or len(candidates) > 40:
            raise RerankingError("RERANKER_INPUT_TOO_LONG")
        with self._lock:
            import torch

            pairs = [[query, item.text] for item in candidates]
            counts = [
                len(ids)
                for ids in self._tokenizer(pairs, padding=False, truncation=False)["input_ids"]
            ]
            if any(n > self.config.max_sequence_tokens for n in counts):
                raise RerankingError("RERANKER_INPUT_TOO_LONG")
            scores: list[float] = []
            try:
                with torch.inference_mode():
                    for start in range(0, len(pairs), self.config.batch_size):
                        encoded = self._tokenizer(
                            pairs[start : start + self.config.batch_size],
                            padding=True,
                            truncation=False,
                            return_tensors="pt",
                        )
                        scores.extend(self._model(**encoded).logits.squeeze(dim=1).tolist())
            except Exception:
                raise RerankingError("RERANKER_INFERENCE_FAILED") from None
            return ordered(
                candidates,
                [
                    RerankResult(
                        chunk_id=item.chunk_id,
                        reranker_score=score,
                        input_hash=input_hash(query, item.text),
                        token_count=count,
                    )
                    for item, score, count in zip(candidates, scores, counts, strict=True)
                ],
            )
