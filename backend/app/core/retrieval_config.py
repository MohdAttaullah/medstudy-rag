"""Versioned M5 retrieval policy.

Three frozen policies, because they version three different things and change classes differ.

* `QueryEncoderConfig` describes what a *query* vector means. It must land in the same vector
  space as the stored article vectors, so a change to it invalidates nothing on disk but changes
  every future query; it is recorded as its own durable version.
* `SparseAnalyzerConfig` describes how text becomes lexical terms. A change to it invalidates the
  built postings: it is **reindex-required**.
* `RetrievalConfig` describes how many candidates each lane returns and how they are fused. It is
  **runtime-safe**, including the BM25 `k1`/`b` parameters — this implementation stores raw term
  frequencies and document lengths rather than pre-weighted scores, so saturation and length
  normalization are applied at query time and can be changed without rebuilding anything.

Every field of each policy participates in the corresponding SHA-256 fingerprint.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.embedding_config import EmbeddingConfig

# Declared on the fields themselves so an operator reading the schema can tell which settings
# take effect immediately and which invalidate a built index.
RUNTIME_SAFE: dict[str, Any] = {"change_class": "runtime-safe"}
REINDEX_REQUIRED: dict[str, Any] = {"change_class": "reindex-required"}
REQUERY_ONLY: dict[str, Any] = {"change_class": "query-side"}


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class QueryEncoderConfig(BaseModel):
    """Dense query-side embedding policy.

    The defaults reproduce the released MedCPT **Query** Encoder semantics: the last hidden state
    of the [CLS] token, 768 dimensions, no L2 normalization, and a 64-token maximum, which is the
    length the released usage example encodes queries at. This is a *different model* from the
    article encoder used for chunks; using the article encoder for questions would place queries
    in the wrong half of the trained pair and is prevented by the pinned `model_id` here.

    Pooling, normalization, dimension and metric are `Literal` for the same reason they are on the
    article side: they define what a vector means, and a different representation needs a
    deliberate code change, a new version record and a benchmark, never an environment variable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="query-encoder-m5-v1", min_length=1, max_length=80)
    model_provider: Literal["huggingface"] = "huggingface"
    model_id: Literal["ncbi/MedCPT-Query-Encoder"] = "ncbi/MedCPT-Query-Encoder"
    model_revision: Literal["d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc"] = (
        "d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc"
    )
    tokenizer_revision: Literal["d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc"] = (
        "d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc"
    )
    # SHA-256 of model.safetensors and tokenizer.json at the pinned revision, both verified
    # before the model is loaded. Measured: this tokenizer.json is byte-identical to the article
    # encoder's, and the query repository's extra added_tokens.json only restates the special
    # tokens already at those vocabulary ids. The checksum is pinned regardless, because a
    # tokenizer that drifted would change how a question is segmented — and therefore what its
    # vector means — while the weights looked untouched.
    model_checksum: Literal["19d78c0d5eaee2f81e6c47c5425bbadcc0c6af016cbb5da4a000d64e59d6e342"] = (
        "19d78c0d5eaee2f81e6c47c5425bbadcc0c6af016cbb5da4a000d64e59d6e342"
    )
    tokenizer_checksum: Literal[
        "6e046044df8a2fcedb10607075dca187cae61d806c0d80a96c5b81017edc90c9"
    ] = "6e046044df8a2fcedb10607075dca187cae61d806c0d80a96c5b81017edc90c9"
    verify_model_checksum: bool = True
    embedding_dimension: Literal[768] = 768
    pooling_strategy: Literal["CLS"] = "CLS"
    normalization: Literal["NONE"] = "NONE"
    distance_metric: Literal["DOT"] = "DOT"
    # The released query usage encodes at 64 tokens. Longer questions are rejected, not shortened.
    max_query_tokens: int = Field(default=64, ge=8, le=512, json_schema_extra=REQUERY_ONLY)
    truncation_policy: Literal["REJECT"] = "REJECT"
    dtype: Literal["float32"] = "float32"
    device: Literal["cpu"] = "cpu"
    torch_threads: int = Field(default=4, ge=1, le=64)
    # Deterministic text preparation only. No rewriting, no expansion, no LLM.
    normalization_version: Literal["query-normalize-v1"] = "query-normalize-v1"
    max_query_characters: int = Field(default=2000, ge=16, le=20000)
    batch_size: int = Field(default=16, ge=1, le=256)
    # Per-process vector cache keyed by policy fingerprint and normalized-query hash. Never
    # persisted, never shared across encoder versions, and it holds no query text.
    cache_size: int = Field(default=256, ge=0, le=10000)
    model_cache_dir: Path | None = None
    offline: bool = False
    # Empty means "load the encoder in this process". The deployed API sets this to the internal
    # retrieval service so the API and dispatcher images never carry torch.
    endpoint: str = ""
    request_timeout_seconds: float = Field(default=30, gt=0, le=300)

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))

    @property
    def vector_semantics(self) -> dict[str, object]:
        """The subset that defines the query vector space, for QueryEncoderVersion identity."""
        return {
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "tokenizer_revision": self.tokenizer_revision,
            "embedding_dimension": self.embedding_dimension,
            "pooling_strategy": self.pooling_strategy,
            "normalization": self.normalization,
            "distance_metric": self.distance_metric,
            "max_query_tokens": self.max_query_tokens,
            "dtype": self.dtype,
            "normalization_version": self.normalization_version,
        }

    @property
    def semantics_fingerprint(self) -> str:
        return _fingerprint(dict(self.vector_semantics))

    def incompatibility(self, article: EmbeddingConfig) -> dict[str, object] | None:
        """Why these query vectors could not be compared with those article vectors, if at all.

        Dense retrieval is a dot product between a query vector and stored article vectors. That
        operation is meaningless unless both sides share dimension, pooling, normalization policy
        and similarity metric, so the mismatch is detected before a search rather than silently
        producing a ranking of nonsense.
        """
        checks = (
            ("embedding_dimension", self.embedding_dimension, article.embedding_dimension),
            ("pooling_strategy", self.pooling_strategy, article.pooling_strategy),
            ("normalization", self.normalization, article.normalization),
            ("distance_metric", self.distance_metric, article.distance_metric),
            ("dtype", self.dtype, article.dtype),
        )
        divergent: dict[str, object] = {
            name: {"query": query, "article": stored}
            for name, query, stored in checks
            if query != stored
        }
        return divergent or None


class SparseAnalyzerConfig(BaseModel):
    """Lexical analysis policy for BM25. Changing any field is reindex-required.

    Biomedical lexical retrieval lives or dies on exact terminology, so the analyzer deliberately
    keeps identifiers intact: `HLA-B27`, `CYP3A4`, `Na+/K+-ATPase`, `HbA1c`, `25-hydroxyvitamin`
    and `1.5 mg/kg` must survive tokenization as recognisable terms rather than being shattered
    into unrelated fragments that no longer match the query.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="sparse-analyzer-m5-v1", min_length=1, max_length=80)
    analyzer_name: Literal["medrag-biomedical"] = "medrag-biomedical"
    analyzer_version: Literal["v1"] = "v1"
    unicode_normalization: Literal["NFKC"] = "NFKC"
    # NORMALIZED_AND_EXACT also emits a case-marked term for any token carrying uppercase, so an
    # exactly-cased identifier match is a distinct, rarer and therefore higher-IDF signal.
    case_policy: Literal["NORMALIZED", "NORMALIZED_AND_EXACT"] = "NORMALIZED_AND_EXACT"
    # WHOLE_AND_PARTS additionally emits the letter-bearing components of a connected identifier,
    # so `HLA-B27` also matches a query mentioning `HLA` without losing the exact whole-term hit.
    compound_policy: Literal["WHOLE", "WHOLE_AND_PARTS"] = "WHOLE_AND_PARTS"
    # Empty by default and deliberately so: a medical question is short, and discarding its
    # ordinary words discards the relationships that distinguish two similar questions.
    stopwords: tuple[str, ...] = ()
    min_term_length: int = Field(default=1, ge=1, le=10)
    max_term_length: int = Field(default=64, ge=8, le=200)
    max_terms_per_chunk: int = Field(default=20000, ge=16, le=1000000)
    # No synonym or abbreviation expansion. M5 establishes whether retrieval works before any
    # query manipulation is layered over it; an expansion lane would need its own evaluation.
    expansion: Literal["NONE"] = "NONE"

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.min_term_length > self.max_term_length:
            raise ValueError("Minimum term length cannot exceed the maximum")
        return self

    @property
    def analyzer_semantics(self) -> dict[str, object]:
        """What determines the produced term set. This is the reindex-required identity."""
        return {
            "analyzer_name": self.analyzer_name,
            "analyzer_version": self.analyzer_version,
            "unicode_normalization": self.unicode_normalization,
            "case_policy": self.case_policy,
            "compound_policy": self.compound_policy,
            "stopwords": list(self.stopwords),
            "min_term_length": self.min_term_length,
            "max_term_length": self.max_term_length,
            "expansion": self.expansion,
        }

    @property
    def analyzer_fingerprint(self) -> str:
        return _fingerprint(dict(self.analyzer_semantics))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class SparseIndexConfig(BaseModel):
    """How the lexical postings are built and reconciled. Mirrors the M4 index policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="sparse-index-m5-v1", min_length=1, max_length=80)
    schema_version: str = Field(default="v1", min_length=1, max_length=20)
    receipt_recovery_seconds: int = Field(default=30, ge=5, le=3600)
    posting_batch_size: int = Field(default=2000, ge=1, le=50000)
    max_chunks: int = Field(default=200000, ge=1, le=5000000)
    verify_postings: bool = True
    timeout_seconds: int = Field(default=1800, ge=30, le=86400)
    lease_seconds: int = Field(default=2400, ge=60, le=172800)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.lease_seconds <= self.timeout_seconds:
            raise ValueError("Lease must exceed the sparse index timeout")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class RetrievalConfig(BaseModel):
    """First-stage candidate policy. Runtime-safe: nothing here is baked into an index.

    The candidate counts and the RRF constant are seeds chosen so the evaluation harness has
    something to run against. They are **not** calibrated results, and the M5 report states which
    values were measured rather than assumed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="retrieval-m5-v1", min_length=1, max_length=80)
    mode: Literal["DENSE_ONLY", "BM25_ONLY", "HYBRID_RRF"] = Field(
        default="HYBRID_RRF", json_schema_extra=RUNTIME_SAFE
    )
    dense_top_k: int = Field(default=40, ge=1, le=1000, json_schema_extra=RUNTIME_SAFE)
    sparse_top_k: int = Field(default=40, ge=1, le=1000, json_schema_extra=RUNTIME_SAFE)
    final_top_k: int = Field(default=20, ge=1, le=500, json_schema_extra=RUNTIME_SAFE)
    # Conventional starting point, benchmarked in the M5 evaluation rather than assumed optimal.
    rrf_k: int = Field(default=60, ge=1, le=1000, json_schema_extra=RUNTIME_SAFE)
    dense_weight: float = Field(default=1.0, ge=0, le=10, json_schema_extra=RUNTIME_SAFE)
    sparse_weight: float = Field(default=1.0, ge=0, le=10, json_schema_extra=RUNTIME_SAFE)
    # Conventional BM25 defaults. Runtime-safe here because postings store raw term frequencies
    # and document lengths; saturation and length normalization are applied when scoring.
    bm25_k1: float = Field(default=1.2, ge=0, le=10, json_schema_extra=RUNTIME_SAFE)
    bm25_b: float = Field(default=0.75, ge=0, le=1, json_schema_extra=RUNTIME_SAFE)
    # Accuracy-first default: if the configured hybrid strategy cannot run, the query fails with a
    # declared error rather than quietly becoming a different retrieval architecture mid-request.
    degradation_policy: Literal["FAIL_CLOSED", "ALLOW_DENSE_ONLY"] = Field(
        default="FAIL_CLOSED", json_schema_extra=RUNTIME_SAFE
    )
    max_scanned_postings: int = Field(default=500000, ge=1000, le=50000000)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    preview_characters: int = Field(default=320, ge=0, le=2000)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.final_top_k > self.dense_top_k + self.sparse_top_k:
            raise ValueError("Final candidate count exceeds the combined lane budget")
        if self.mode == "HYBRID_RRF" and self.dense_weight == 0 and self.sparse_weight == 0:
            raise ValueError("Hybrid fusion needs at least one lane weighted above zero")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))
