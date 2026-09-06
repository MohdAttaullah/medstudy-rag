"""Versioned M4 policy.

Two frozen policies, because they version different things. `EmbeddingConfig` describes what a
vector *means* — model, revision, pooling, dimension, normalization — and a change to it must
produce a new EmbeddingVersion and new vectors. `IndexConfig` describes how those vectors are
stored and reconciled in the vector database, and a change to it must produce a new index run.

Every field of both participates in the corresponding SHA-256 fingerprint.
"""

import hashlib
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _fingerprint(model: BaseModel) -> str:
    return hashlib.sha256(
        json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class EmbeddingConfig(BaseModel):
    """Dense document-side embedding policy.

    The defaults reproduce the released MedCPT Article Encoder semantics: the last hidden state of
    the [CLS] token, 768 dimensions, 512 input tokens, no L2 normalization, and inner-product
    similarity. Changing pooling or normalization changes what every stored vector means, so those
    fields are `Literal` — a different representation needs a deliberate code change, a new
    configuration version and a benchmark, not an environment variable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="embedding-m4-v1", min_length=1, max_length=80)
    model_provider: Literal["huggingface"] = "huggingface"
    model_id: Literal["ncbi/MedCPT-Article-Encoder"] = "ncbi/MedCPT-Article-Encoder"
    model_revision: Literal["d05a736da4bb84ee4057b7f7999485be6ed85465"] = (
        "d05a736da4bb84ee4057b7f7999485be6ed85465"
    )
    tokenizer_revision: Literal["d05a736da4bb84ee4057b7f7999485be6ed85465"] = (
        "d05a736da4bb84ee4057b7f7999485be6ed85465"
    )
    # SHA-256 of model.safetensors at the pinned revision, verified before the model is loaded.
    model_checksum: Literal["a5d5ffe4d8666c1d0aa15f371b94fc3492ca8f927e5621abd4b3ee9fc845b0f3"] = (
        "a5d5ffe4d8666c1d0aa15f371b94fc3492ca8f927e5621abd4b3ee9fc845b0f3"
    )
    verify_model_checksum: bool = True
    embedding_dimension: Literal[768] = 768
    pooling_strategy: Literal["CLS"] = "CLS"
    normalization: Literal["NONE"] = "NONE"
    distance_metric: Literal["DOT"] = "DOT"
    max_input_tokens: Literal[512] = 512
    dtype: Literal["float32"] = "float32"
    device: Literal["cpu"] = "cpu"
    torch_threads: int = Field(default=4, ge=1, le=64)
    # REJECT is the only policy implemented. Truncation would silently drop medical evidence from
    # the indexed representation while still presenting the chunk as retrievable.
    truncation_policy: Literal["REJECT"] = "REJECT"
    input_builder_version: Literal["medcpt-two-field-v1"] = "medcpt-two-field-v1"
    context_separator: Literal["\n\n"] = "\n\n"
    max_context_characters: int = Field(default=300, ge=0, le=2000)
    eligible_chunk_types: tuple[str, ...] = (
        "TEXT_CHILD",
        "LIST",
        "TABLE",
        "TABLE_PART",
        "FORMULA",
        "FIGURE_CONTEXT",
        "QUESTION",
        "QUESTION_EXPLANATION",
        "OTHER_STRUCTURED",
    )
    # TEXT_PARENT is a context-expansion unit for a later milestone, not a first-stage retrieval
    # unit. Embedding it here would put whole sections in competition with the precise children.
    embed_parent_chunks: Literal[False] = False
    batch_size: int = Field(default=16, ge=1, le=256)
    max_batch_tokens: int = Field(default=8192, ge=512, le=131072)
    reuse_existing_embeddings: bool = True
    max_chunks: int = Field(default=200000, ge=1, le=5000000)
    model_cache_dir: Path | None = None
    offline: bool = False
    load_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    lease_seconds: int = Field(default=4200, ge=120, le=172800)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.lease_seconds <= self.timeout_seconds:
            raise ValueError("Lease must exceed the embedding timeout")
        if not self.eligible_chunk_types:
            raise ValueError("At least one chunk type must be retrieval eligible")
        if "TEXT_PARENT" in self.eligible_chunk_types:
            raise ValueError("Parent chunks are context units, not first-stage retrieval units")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self)

    @property
    def vector_semantics(self) -> dict[str, object]:
        """The subset that defines what a vector means, for EmbeddingVersion identity."""
        return {
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "tokenizer_revision": self.tokenizer_revision,
            "embedding_dimension": self.embedding_dimension,
            "pooling_strategy": self.pooling_strategy,
            "normalization": self.normalization,
            "distance_metric": self.distance_metric,
            "max_input_tokens": self.max_input_tokens,
            "dtype": self.dtype,
            "input_builder_version": self.input_builder_version,
            "context_separator": self.context_separator,
            "max_context_characters": self.max_context_characters,
        }

    @property
    def semantics_fingerprint(self) -> str:
        """Identity of the vector space itself, independent of batching or resource settings."""
        return hashlib.sha256(
            json.dumps(self.vector_semantics, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class IndexConfig(BaseModel):
    """Vector-index storage and reconciliation policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="index-m4-v1", min_length=1, max_length=80)
    schema_version: str = Field(default="v1", min_length=1, max_length=20)
    collection_prefix: str = Field(default="medrag_chunks", min_length=1, max_length=60)
    alias: str = Field(default="medrag_chunks_active", min_length=1, max_length=80)
    # Named from the start so BM25 sparse and late-interaction vectors can be added later to the
    # same points without changing point identity.
    dense_vector_name: Literal["medcpt_dense"] = "medcpt_dense"
    upsert_batch_size: int = Field(default=64, ge=1, le=1000)
    verify_batch_size: int = Field(default=128, ge=1, le=1000)
    # Full verification reads every point back. A sample is available for very large corpora, but
    # the count and identity checks always cover the whole run.
    verify_vectors: bool = True
    verify_sample_ratio: float = Field(default=1.0, gt=0, le=1)
    request_timeout_seconds: int = Field(default=60, ge=5, le=600)
    upsert_retries: int = Field(default=3, ge=0, le=10)
    payload_indexes: tuple[str, ...] = (
        "tenant_id",
        "document_id",
        "document_version_id",
        "chunk_type",
        "source_type",
        "authority_level",
        "embedding_run_id",
    )
    tenant_payload_key: Literal["tenant_id"] = "tenant_id"

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.tenant_payload_key not in self.payload_indexes:
            raise ValueError("The tenant key must be a payload index; retrieval always filters it")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self)

    def collection(self, semantics_fingerprint: str) -> str:
        """Physical collection name.

        The vector semantics are part of the name, so an incompatible model or pooling change
        builds a *new* collection beside the old one and the alias switches only after the
        replacement verifies.
        """
        return f"{self.collection_prefix}_{self.schema_version}_{semantics_fingerprint[:12]}"
