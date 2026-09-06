"""Embedding technical-quality evaluation.

This measures whether the encoder and the input builder behave as the configuration says they do:
the pinned revision is loaded, inputs are constructed deterministically, vectors have the declared
shape and are finite, the token limit is enforced rather than silently applied, batching agrees
with single inference, and repeated inference on one host is stable.

It is **not** retrieval evaluation. There is no query, no index, no ranking and no Recall@K here;
those become meaningful only once query-side retrieval exists. It is also not a medical accuracy
measure: one coarse ordering check confirms the encoder is not catastrophically broken, and
nothing is tuned against it.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from app.core.embedding_config import EmbeddingConfig
from app.embeddings.inputs import ChunkSource, build, eligible
from app.embeddings.model import EmbeddingModel
from app.embeddings.validation import check_vector

GOLD_SCHEMA = "embedding-gold-m4-v1"
NAMESPACE = UUID("2f1b8a0e-9c1d-4a7e-8a4b-6a5c0d2e1f30")


@dataclass(frozen=True)
class GoldCase:
    """One synthetic chunk and what the embedding system must do with it."""

    id: str
    chunk_type: str
    retrieval_text: str
    document_title: str = "Synthetic reference"
    hierarchy: list[str] = field(default_factory=list)
    caption: str | None = None
    expect_eligible: bool = True
    expect_over_limit: bool = False
    same_input_as: str | None = None
    different_input_from: str | None = None

    @property
    def chunk_id(self) -> UUID:
        return uuid5(NAMESPACE, self.id)

    def source(self) -> ChunkSource:
        return ChunkSource(
            chunk_id=self.chunk_id,
            chunk_type=self.chunk_type,
            retrieval_text=self.retrieval_text,
            document_title=self.document_title,
            hierarchy=tuple(self.hierarchy),
            caption=self.caption,
        )


@dataclass(frozen=True)
class SimilarityCase:
    """A coarse ordering check. Not retrieval accuracy, and nothing is tuned against it."""

    id: str
    anchor: str
    nearer: str
    farther: str


@dataclass(frozen=True)
class CaseResult:
    name: str
    checks: int
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class Report:
    results: tuple[CaseResult, ...]
    specification: dict[str, Any]

    @property
    def cases(self) -> int:
        return len(self.results)

    @property
    def passed_cases(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def checks(self) -> int:
        return sum(result.checks for result in self.results)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"{result.name}: {failure}" for result in self.results for failure in result.failures
        )

    def render(self) -> str:
        lines = [
            "Embedding technical-quality evaluation (not retrieval or medical accuracy)",
            f"model:     {self.specification['model_id']} @ "
            f"{self.specification['model_revision'][:12]}",
            f"vectors:   {self.specification['dimension']}d, "
            f"{self.specification['pooling']} pooling, "
            f"{self.specification['normalization']} normalization, "
            f"{self.specification['distance_metric']} similarity",
            f"cases:     {self.passed_cases}/{self.cases} passed",
            f"checks:    {self.checks - len(self.failures)}/{self.checks} passed",
        ]
        lines += [f"  FAIL {failure}" for failure in self.failures]
        return "\n".join(lines)


def load_gold(path: Path) -> tuple[tuple[GoldCase, ...], tuple[SimilarityCase, ...]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != GOLD_SCHEMA:
        raise ValueError(f"Unsupported gold dataset schema: {payload.get('schema')!r}")
    return (
        tuple(GoldCase(**case) for case in payload["chunks"]),
        tuple(SimilarityCase(**case) for case in payload.get("similarity", [])),
    )


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def evaluate(
    cases: tuple[GoldCase, ...],
    similarity: tuple[SimilarityCase, ...],
    model: EmbeddingModel,
    config: EmbeddingConfig,
) -> Report:
    specification = model.specification
    results: list[CaseResult] = []

    class Case:
        """Accumulates the checks of one named case and records its result when finished."""

        def __init__(self, name: str) -> None:
            self.name: str = name
            self.checks: int = 0
            self.failures: list[str] = []

        def __call__(self, condition: bool, message: str) -> None:
            self.checks += 1
            if not condition:
                self.failures.append(message)

        def finish(self) -> None:
            results.append(CaseResult(self.name, self.checks, tuple(self.failures)))

    def case(name: str) -> Case:
        return Case(name)

    # ------------------------------------------------------------------ model identity
    check = case("model-pinning")
    check(specification.model_id == config.model_id, "model id is not the configured one")
    check(
        specification.model_revision == config.model_revision,
        f"revision {specification.model_revision} != pinned {config.model_revision}",
    )
    check(
        specification.model_checksum == config.model_checksum,
        "loaded weights do not match the pinned checksum",
    )
    check(specification.dimension == config.embedding_dimension, "dimension is not as configured")
    check(specification.pooling == config.pooling_strategy, "pooling is not as configured")
    check(specification.normalization == config.normalization, "normalization is not as configured")
    check(
        specification.distance_metric == config.distance_metric,
        "similarity metric is not as configured",
    )
    check(
        specification.max_input_tokens == config.max_input_tokens,
        "input limit is not as configured",
    )
    check.finish()

    # ------------------------------------------------------------------ eligibility and inputs
    check = case("eligibility")
    for entry in cases:
        check(
            eligible(entry.chunk_type, config) is entry.expect_eligible,
            f"{entry.id}: {entry.chunk_type} eligibility is not {entry.expect_eligible}",
        )
    check.finish()

    inputs = {entry.id: build(entry.source(), config) for entry in cases}
    check = case("input-determinism")
    for entry in cases:
        repeated = build(entry.source(), config)
        check(
            repeated.input_hash == inputs[entry.id].input_hash,
            f"{entry.id}: rebuilding the same source produced a different input hash",
        )
        check(
            repeated.context == inputs[entry.id].context and repeated.body == inputs[entry.id].body,
            f"{entry.id}: rebuilding the same source produced different fields",
        )
        check(
            inputs[entry.id].body == entry.retrieval_text,
            f"{entry.id}: the body is not the retrieval representation verbatim",
        )
        if entry.same_input_as:
            check(
                inputs[entry.id].input_hash == inputs[entry.same_input_as].input_hash,
                f"{entry.id}: identical content did not produce an identical input hash",
            )
        if entry.different_input_from:
            check(
                inputs[entry.id].input_hash != inputs[entry.different_input_from].input_hash,
                f"{entry.id}: different content collided with {entry.different_input_from}",
            )
    check.finish()

    # ------------------------------------------------------------------ token limit
    check = case("token-limit")
    measurable = tuple(inputs[entry.id] for entry in cases)
    measured = {item.chunk_id: item for item in model.measure(measurable)}
    for entry in cases:
        item = measured[entry.chunk_id]
        check(
            item.exceeds_limit is entry.expect_over_limit,
            f"{entry.id}: over-limit is {item.exceeds_limit}, expected {entry.expect_over_limit}",
        )
        check(item.token_count > 0, f"{entry.id}: token count is not positive")
    check.finish()

    embeddable = tuple(
        inputs[entry.id] for entry in cases if entry.expect_eligible and not entry.expect_over_limit
    )
    rejected = tuple(inputs[entry.id] for entry in cases if entry.expect_over_limit)

    check = case("truncation-policy")
    if rejected:
        try:
            model.embed_documents(rejected)
            check(False, "an over-long input was embedded instead of rejected")
        except Exception as error:  # noqa: BLE001 - any refusal is acceptable, silence is not
            check(
                "TOO_LONG" in str(error) or "too long" in str(error).lower(),
                f"an over-long input failed for an unrelated reason: {error}",
            )
    else:
        check(True, "")
    check.finish()

    # ------------------------------------------------------------------ vectors
    vectors = {vector.chunk_id: vector for vector in model.embed_documents(embeddable)}
    check = case("vector-shape")
    for value in embeddable:
        vector = vectors.get(value.chunk_id)
        if vector is None:
            check(False, "a vector is missing for an embeddable input")
            continue
        check(
            len(vector.values) == config.embedding_dimension,
            f"vector has {len(vector.values)} dimensions, expected {config.embedding_dimension}",
        )
        check(
            all(math.isfinite(number) for number in vector.values),
            "vector contains non-finite values",
        )
        check(any(number != 0.0 for number in vector.values), "vector is entirely zero")
        check(check_vector(vector, config) == [], "vector failed the sanity rules")
        check(vector.token_count <= config.max_input_tokens, "token count exceeds the limit")
    check.finish()

    check = case("repeatability")
    second_pass = {vector.chunk_id: vector for vector in model.embed_documents(embeddable)}
    for identity, vector in vectors.items():
        check(
            second_pass[identity].checksum == vector.checksum,
            "repeated inference on the same host produced different bytes",
        )
    check.finish()

    check = case("batch-equivalence")
    for value in embeddable:
        alone = model.embed_documents((value,))[0]
        grouped = vectors[value.chunk_id]
        delta = max(abs(a - b) for a, b in zip(alone.values, grouped.values, strict=True))
        cosine = _dot(alone.values, grouped.values) / (alone.norm * grouped.norm)
        # Padding differs between a batch and a single input, so the last bits of float
        # arithmetic differ. Numerical equivalence is asserted; byte identity is not claimed.
        check(delta < 1e-4, f"batched and single inference differ by {delta:.2e}")
        check(abs(cosine - 1.0) < 1e-6, f"batched and single cosine agreement is {cosine:.9f}")
    check.finish()

    # ------------------------------------------------------------------ coarse ordering
    check = case("similarity-sanity")
    for pair in similarity:
        texts = {"anchor": pair.anchor, "nearer": pair.nearer, "farther": pair.farther}
        produced = {
            name: model.embed_documents(
                (
                    build(
                        ChunkSource(
                            chunk_id=uuid5(NAMESPACE, pair.id + name),
                            chunk_type="TEXT_CHILD",
                            retrieval_text=text,
                            document_title="Synthetic reference",
                        ),
                        config,
                    ),
                )
            )[0].values
            for name, text in texts.items()
        }
        near = _dot(produced["anchor"], produced["nearer"])
        far = _dot(produced["anchor"], produced["farther"])
        check(
            near > far,
            f"{pair.id}: a near paraphrase scored {near:.2f}, not above unrelated {far:.2f}",
        )
    check.finish()

    return Report(
        tuple(results),
        {
            "model_id": specification.model_id,
            "model_revision": specification.model_revision,
            "dimension": specification.dimension,
            "pooling": specification.pooling,
            "normalization": specification.normalization,
            "distance_metric": specification.distance_metric,
            "library_versions": specification.library_versions,
        },
    )
