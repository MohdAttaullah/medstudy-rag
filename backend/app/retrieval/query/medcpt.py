"""MedCPT Query Encoder adapter.

The only module in the codebase that imports transformers or torch for query encoding. It
reproduces the released MedCPT query-side representation: a single text field, the last hidden
state of the [CLS] token, 768 dimensions, no L2 normalization, and the 64-token maximum the
released usage encodes queries at.

This is deliberately a **different checkpoint** from the article encoder in `app.embeddings`.
MedCPT was trained as a query/article pair, and the two encoders occupy the same output space
only when each is used on its own side. Encoding a question with the article encoder would place
it in the wrong half of that pair and quietly degrade every ranking, so the model id is pinned by
a `Literal` and the loaded model's own configuration is checked against it.

Deliberate omissions: no mean pooling, no normalization, no query rewriting, no instruction
prefix. Each would change what a query vector means and needs its own benchmark and version.
"""

import hashlib
import os
import struct
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core.retrieval_config import QueryEncoderConfig
from app.retrieval.errors import RetrievalError
from app.retrieval.model import QueryEncoderSpec, QueryMeasurement, QueryVector
from app.retrieval.query.normalize import normalize, query_hash

WEIGHTS = "model.safetensors"
TOKENIZER = "tokenizer.json"


def _versions() -> dict[str, str]:
    from importlib.metadata import version

    result = {}
    for package in ("transformers", "torch", "tokenizers", "safetensors", "numpy"):
        try:
            result[package] = version(package)
        except Exception:  # noqa: BLE001 - a missing optional package is reported, not fatal
            result[package] = "unknown"
    return result


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def vector_checksum(values: tuple[float, ...]) -> str:
    """Checksum of the canonical little-endian float32 encoding, as on the article side.

    An identity of bytes, used for cache and transport integrity and for the host/container
    comparison. It is never a claim about cross-machine reproducibility or about quality.
    """
    return hashlib.sha256(struct.pack(f"<{len(values)}f", *values)).hexdigest()


class MedCPTQueryEncoder:
    """Loads the pinned query encoder from a local cache and encodes questions on CPU."""

    def __init__(self, config: QueryEncoderConfig) -> None:
        self.config = config
        self._model: Any = None
        self._tokenizer: Any = None
        self._spec: QueryEncoderSpec | None = None
        self._cache: OrderedDict[str, QueryVector] = OrderedDict()

    # ------------------------------------------------------------------ loading

    def _resolve(self) -> Path:
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
                        TOKENIZER,
                        "tokenizer_config.json",
                        "vocab.txt",
                        "special_tokens_map.json",
                        "added_tokens.json",
                    ],
                )
            )
        except LocalEntryNotFoundError as exc:
            raise RetrievalError("QUERY_ENCODER_UNAVAILABLE_OFFLINE") from exc
        except (HfHubHTTPError, OSError) as exc:
            raise RetrievalError("QUERY_ENCODER_UNAVAILABLE") from exc

    def load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModel, AutoTokenizer

        path = self._resolve()
        weights, tokenizer_file = path / WEIGHTS, path / TOKENIZER
        if not weights.exists() or not tokenizer_file.exists():
            raise RetrievalError("QUERY_ENCODER_UNAVAILABLE")
        model_digest = checksum(weights)
        tokenizer_digest = checksum(tokenizer_file)
        if self.config.verify_model_checksum and (
            model_digest != self.config.model_checksum
            or tokenizer_digest != self.config.tokenizer_checksum
        ):
            # Both files are pinned: a tokenizer that drifts changes how a question is segmented
            # and therefore what the vector means, even when the weights are untouched.
            raise RetrievalError("QUERY_ENCODER_CHECKSUM_MISMATCH")

        torch.set_num_threads(self.config.torch_threads)
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
            model = AutoModel.from_pretrained(
                str(path), local_files_only=True, use_safetensors=True
            )
        except Exception as exc:  # noqa: BLE001 - vendor exceptions map to a safe code
            raise RetrievalError("QUERY_ENCODER_UNAVAILABLE") from exc

        hidden = int(getattr(model.config, "hidden_size", 0))
        limit = int(getattr(model.config, "max_position_embeddings", 0))
        if hidden != self.config.embedding_dimension or limit < self.config.max_query_tokens:
            raise RetrievalError(
                "QUERY_ENCODER_REVISION_MISMATCH",
                {"expected_dimension": self.config.embedding_dimension, "loaded": hidden},
            )
        model.eval()
        model.to(self.config.device)
        self._model = model
        self._spec = QueryEncoderSpec(
            provider=self.config.model_provider,
            model_id=self.config.model_id,
            model_revision=self.config.model_revision,
            tokenizer_revision=self.config.tokenizer_revision,
            model_checksum=model_digest,
            tokenizer_checksum=tokenizer_digest,
            dimension=hidden,
            pooling=self.config.pooling_strategy,
            normalization=self.config.normalization,
            distance_metric=self.config.distance_metric,
            max_query_tokens=self.config.max_query_tokens,
            dtype=self.config.dtype,
            device=self.config.device,
            normalization_version=self.config.normalization_version,
            semantics_fingerprint=self.config.semantics_fingerprint,
            library_versions=_versions(),
        )

    @property
    def specification(self) -> QueryEncoderSpec:
        self.load()
        assert self._spec is not None
        return self._spec

    # ------------------------------------------------------------------ inference

    def _prepare(self, queries: Sequence[str]) -> list[str]:
        prepared = []
        for text in queries:
            if len(text) > self.config.max_query_characters:
                raise RetrievalError(
                    "QUERY_TOO_LONG",
                    {
                        "characters": len(text),
                        "character_limit": self.config.max_query_characters,
                    },
                )
            normalized = normalize(text, self.config)
            if not normalized:
                raise RetrievalError("QUERY_EMPTY")
            prepared.append(normalized)
        return prepared

    def measure(self, queries: Sequence[str]) -> tuple[QueryMeasurement, ...]:
        """Token length under the encoder's own tokenizer, including its special tokens."""
        self.load()
        if not queries:
            return ()
        encodings = self._tokenizer(self._prepare(queries), truncation=False, padding=False)
        return tuple(
            QueryMeasurement(
                token_count=len(ids), exceeds_limit=len(ids) > self.config.max_query_tokens
            )
            for ids in encodings["input_ids"]
        )

    def encode_queries(self, queries: Sequence[str]) -> tuple[QueryVector, ...]:
        self.load()
        if not queries:
            return ()
        import torch

        prepared = self._prepare(queries)
        measurements = self.measure(queries)
        over = [
            (index, measurement)
            for index, measurement in enumerate(measurements)
            if measurement.exceeds_limit
        ]
        if over:
            # Reject rather than truncate. A silently shortened question would be answered from
            # evidence retrieved for a different, shorter question than the one that was asked.
            raise RetrievalError(
                "QUERY_TOO_LONG",
                {
                    "limit": self.config.max_query_tokens,
                    "observed": max(measurement.token_count for _, measurement in over),
                    "positions": [index for index, _ in over],
                },
            )

        hashes = [query_hash(text, self.config) for text in prepared]
        cached = {
            position: self._cache[digest]
            for position, digest in enumerate(hashes)
            if digest in self._cache
        }
        pending = [position for position in range(len(prepared)) if position not in cached]
        produced: dict[int, QueryVector] = {}
        if pending:
            try:
                with torch.no_grad():
                    batch = self._tokenizer(
                        [prepared[position] for position in pending],
                        truncation=False,
                        padding=True,
                        return_tensors="pt",
                    )
                    # [CLS] last hidden state, as released. Index 0 is the [CLS] position.
                    hidden = self._model(**batch).last_hidden_state[:, 0, :]
                    vectors = hidden.to(torch.float32).cpu()
            except MemoryError as exc:
                raise RetrievalError("QUERY_ENCODING_FAILED") from exc
            except Exception as exc:  # noqa: BLE001 - vendor exceptions map to a safe code
                raise RetrievalError("QUERY_ENCODING_FAILED") from exc

            if not bool(torch.isfinite(vectors).all()):
                raise RetrievalError("QUERY_VECTOR_INVALID", {"reason": "non-finite"})
            if vectors.shape[1] != self.config.embedding_dimension:
                raise RetrievalError(
                    "QUERY_VECTOR_INVALID",
                    {
                        "expected_dimension": self.config.embedding_dimension,
                        "produced": int(vectors.shape[1]),
                    },
                )
            norms = torch.linalg.vector_norm(vectors, dim=1)
            for offset, position in enumerate(pending):
                values = tuple(float(number) for number in vectors[offset].tolist())
                produced[position] = QueryVector(
                    values=values,
                    token_count=measurements[position].token_count,
                    normalized=prepared[position],
                    query_hash=hashes[position],
                    checksum=vector_checksum(values),
                    norm=float(norms[offset]),
                )

        result = []
        for position in range(len(prepared)):
            if position in cached:
                result.append(
                    QueryVector(
                        values=cached[position].values,
                        token_count=cached[position].token_count,
                        normalized=prepared[position],
                        query_hash=hashes[position],
                        checksum=cached[position].checksum,
                        norm=cached[position].norm,
                        cached=True,
                    )
                )
                continue
            vector = produced[position]
            self._remember(hashes[position], vector)
            result.append(vector)
        return tuple(result)

    def _remember(self, digest: str, vector: QueryVector) -> None:
        """Bounded in-process cache keyed by encoder version and normalized-query hash.

        The key includes the pinned model and normalization version, so a vector produced under a
        different encoder can never be served for a query under this one. Nothing is persisted and
        no question text is stored.
        """
        if self.config.cache_size <= 0:
            return
        self._cache[digest] = replace(vector, normalized="")
        self._cache.move_to_end(digest)
        while len(self._cache) > self.config.cache_size:
            self._cache.popitem(last=False)


def configure_offline_environment(config: QueryEncoderConfig) -> None:
    """Make the offline intent explicit to the huggingface libraries as well as to our own code."""
    if config.offline:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
