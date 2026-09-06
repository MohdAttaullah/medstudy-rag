"""MedCPT Article Encoder adapter.

The only module in the codebase that imports transformers or torch for embedding. It reproduces
the released MedCPT article-side representation: a two-field input, the last hidden state of the
[CLS] token, 768 dimensions, a 512-token maximum, no L2 normalization. Similarity is therefore
inner product, which is why the collection is created with DOT distance.

Deliberate omissions: no mean pooling, no normalization, no query-side encoder, no dimensionality
reduction. Each would change what every stored vector means and needs its own benchmark, ADR and
EmbeddingVersion.
"""

import hashlib
import os
import struct
from pathlib import Path
from typing import Any

from app.core.embedding_config import EmbeddingConfig
from app.embeddings.errors import EmbeddingError
from app.embeddings.inputs import encoded
from app.embeddings.model import (
    EmbeddingInput,
    EmbeddingModelSpec,
    EmbeddingVector,
    TokenMeasurement,
)

WEIGHTS = "model.safetensors"


def _versions() -> dict[str, str]:
    from importlib.metadata import version

    result = {}
    for package in ("transformers", "torch", "tokenizers", "safetensors", "numpy"):
        try:
            result[package] = version(package)
        except Exception:  # noqa: BLE001 - a missing optional package is reported, not fatal here
            result[package] = "unknown"
    return result


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def vector_checksum(values: tuple[float, ...]) -> str:
    """Deterministic checksum of the canonical little-endian float32 encoding of a vector.

    Used for duplicate-delivery verification, corruption detection and host/container comparison.
    It is an identity of bytes, never a measure of embedding quality.
    """
    return hashlib.sha256(struct.pack(f"<{len(values)}f", *values)).hexdigest()


class MedCPTArticleEmbedder:
    """Loads the pinned revision from a local cache and embeds document-side chunks on CPU."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self._model: Any = None
        self._tokenizer: Any = None
        self._spec: EmbeddingModelSpec | None = None

    # ------------------------------------------------------------------ loading

    def _resolve(self) -> Path:
        """Locate the pinned snapshot in the configured cache, downloading only if permitted."""
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError

        cache = self.config.model_cache_dir
        try:
            return Path(
                snapshot_download(
                    repo_id=self.config.model_id,
                    revision=self.config.model_revision,
                    cache_dir=str(cache) if cache else None,
                    local_files_only=self.config.offline,
                    allow_patterns=[
                        "config.json",
                        WEIGHTS,
                        "tokenizer.json",
                        "tokenizer_config.json",
                        "vocab.txt",
                        "special_tokens_map.json",
                    ],
                )
            )
        except LocalEntryNotFoundError as exc:
            raise EmbeddingError("EMBEDDING_MODEL_UNAVAILABLE_OFFLINE") from exc
        except (HfHubHTTPError, OSError) as exc:
            raise EmbeddingError("EMBEDDING_MODEL_LOAD_FAILED") from exc

    def load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModel, AutoTokenizer

        path = self._resolve()
        weights = path / WEIGHTS
        if not weights.exists():
            raise EmbeddingError("EMBEDDING_MODEL_LOAD_FAILED")
        digest = checksum(weights)
        if self.config.verify_model_checksum and digest != self.config.model_checksum:
            raise EmbeddingError("EMBEDDING_MODEL_CHECKSUM_MISMATCH")
        tokenizer_file = path / "tokenizer.json"
        if not tokenizer_file.exists():
            raise EmbeddingError("EMBEDDING_TOKENIZER_MISMATCH")

        torch.set_num_threads(self.config.torch_threads)
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
            model = AutoModel.from_pretrained(
                str(path), local_files_only=True, use_safetensors=True
            )
        except Exception as exc:  # noqa: BLE001 - vendor exceptions are mapped to a safe code
            raise EmbeddingError("EMBEDDING_MODEL_LOAD_FAILED") from exc

        hidden = int(getattr(model.config, "hidden_size", 0))
        limit = int(getattr(model.config, "max_position_embeddings", 0))
        if hidden != self.config.embedding_dimension:
            raise EmbeddingError(
                "EMBEDDING_DIMENSION_MISMATCH",
                {"expected": self.config.embedding_dimension, "loaded": hidden},
            )
        if limit < self.config.max_input_tokens:
            raise EmbeddingError("EMBEDDING_MODEL_REVISION_MISMATCH")
        model.eval()
        model.to(self.config.device)
        self._model = model
        self._spec = EmbeddingModelSpec(
            provider=self.config.model_provider,
            model_id=self.config.model_id,
            model_revision=self.config.model_revision,
            tokenizer_revision=self.config.tokenizer_revision,
            model_checksum=digest,
            dimension=hidden,
            pooling=self.config.pooling_strategy,
            normalization=self.config.normalization,
            distance_metric=self.config.distance_metric,
            max_input_tokens=self.config.max_input_tokens,
            dtype=self.config.dtype,
            device=self.config.device,
            library_versions=_versions(),
        )

    @property
    def specification(self) -> EmbeddingModelSpec:
        self.load()
        assert self._spec is not None
        return self._spec

    # ------------------------------------------------------------------ inference

    def _pairs(self, inputs: tuple[EmbeddingInput, ...]) -> tuple[list[str], list[str]]:
        fields = [encoded(value, self.config) for value in inputs]
        return [first for first, _ in fields], [second for _, second in fields]

    def measure(self, inputs: tuple[EmbeddingInput, ...]) -> tuple[TokenMeasurement, ...]:
        """Token length under the model's own tokenizer, including its special tokens.

        Measured before inference so an over-long chunk is reported as a finding rather than
        quietly truncated to whatever fits.
        """
        self.load()
        if not inputs:
            return ()
        first, second = self._pairs(inputs)
        encodings = self._tokenizer(first, second, truncation=False, padding=False)
        return tuple(
            TokenMeasurement(
                chunk_id=value.chunk_id,
                token_count=len(ids),
                exceeds_limit=len(ids) > self.config.max_input_tokens,
            )
            for value, ids in zip(inputs, encodings["input_ids"], strict=True)
        )

    def embed_documents(self, inputs: tuple[EmbeddingInput, ...]) -> tuple[EmbeddingVector, ...]:
        self.load()
        if not inputs:
            return ()
        import torch

        measurements = self.measure(inputs)
        over = [m for m in measurements if m.exceeds_limit]
        if over:
            # Fail rather than truncate. A silently shortened input would index a chunk whose
            # vector represents only part of the evidence the chunk claims to carry.
            raise EmbeddingError(
                "EMBEDDING_INPUT_TOO_LONG",
                {
                    "chunk_ids": [str(m.chunk_id) for m in over],
                    "limit": self.config.max_input_tokens,
                    "observed": max(m.token_count for m in over),
                },
            )

        first, second = self._pairs(inputs)
        try:
            with torch.no_grad():
                batch = self._tokenizer(
                    first,
                    second,
                    truncation=True,
                    padding=True,
                    return_tensors="pt",
                    max_length=self.config.max_input_tokens,
                )
                # [CLS] last hidden state, as released. Index 0 is the [CLS] position.
                hidden = self._model(**batch).last_hidden_state[:, 0, :]
                vectors = hidden.to(torch.float32).cpu()
        except torch.cuda.OutOfMemoryError as exc:  # pragma: no cover - CPU deployment
            raise EmbeddingError("EMBEDDING_OOM") from exc
        except MemoryError as exc:
            raise EmbeddingError("EMBEDDING_OOM") from exc
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 - vendor exceptions are mapped to a safe code
            raise EmbeddingError("EMBEDDING_INFERENCE_FAILED") from exc

        if not bool(torch.isfinite(vectors).all()):
            raise EmbeddingError("EMBEDDING_NON_FINITE")
        if vectors.shape[1] != self.config.embedding_dimension:
            raise EmbeddingError(
                "EMBEDDING_DIMENSION_MISMATCH",
                {"expected": self.config.embedding_dimension, "produced": int(vectors.shape[1])},
            )

        norms = torch.linalg.vector_norm(vectors, dim=1)
        result = []
        for value, measurement, row, norm in zip(inputs, measurements, vectors, norms, strict=True):
            values = tuple(float(number) for number in row.tolist())
            result.append(
                EmbeddingVector(
                    chunk_id=value.chunk_id,
                    values=values,
                    token_count=measurement.token_count,
                    checksum=vector_checksum(values),
                    norm=float(norm),
                )
            )
        return tuple(result)


def configure_offline_environment(config: EmbeddingConfig) -> None:
    """Make the offline intent explicit to the huggingface libraries as well as to our own code."""
    if config.offline:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
