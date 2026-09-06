"""Provider-independent embedding contracts.

Nothing here imports torch, transformers or huggingface_hub. Domain services depend on this
module; only `medcpt.py` depends on the machine-learning stack, so replacing the encoder is one
module of work and cannot leak inference details into orchestration.
"""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EmbeddingModelSpec:
    """What a vector produced by this model means, as reported by the loaded model itself."""

    provider: str
    model_id: str
    model_revision: str
    tokenizer_revision: str
    model_checksum: str
    dimension: int
    pooling: str
    normalization: str
    distance_metric: str
    max_input_tokens: int
    dtype: str
    device: str
    library_versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    """One deterministic model input, already built from persisted M3 content.

    `context` and `body` are kept apart because MedCPT is trained on a two-field article
    representation. `input_hash` covers exactly what the model will see, so a reused vector can be
    proved to correspond to this input rather than merely to a similar-looking chunk.
    """

    chunk_id: UUID
    context: str
    body: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    chunk_id: UUID
    values: tuple[float, ...]
    token_count: int
    checksum: str
    norm: float


@dataclass(frozen=True, slots=True)
class TokenMeasurement:
    chunk_id: UUID
    token_count: int
    exceeds_limit: bool


class EmbeddingModel(Protocol):
    @property
    def specification(self) -> EmbeddingModelSpec: ...

    def measure(self, inputs: tuple[EmbeddingInput, ...]) -> tuple[TokenMeasurement, ...]:
        """Token counts under the model's own tokenizer, without running inference."""
        ...

    def embed_documents(self, inputs: tuple[EmbeddingInput, ...]) -> tuple[EmbeddingVector, ...]:
        """Document-side vectors, in input order. Never silently truncates."""
        ...
