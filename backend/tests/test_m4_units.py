"""M4 unit tests: model pinning, input construction, eligibility, vector sanity, reconciliation.

These use a deterministic fake encoder wherever the assertion is about *our* logic, and the real
MedCPT encoder only where the assertion is genuinely about the model. That keeps the fast suite
fast and keeps the model-dependent claims honest about what they measured.
"""

import math
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from app.core.embedding_config import EmbeddingConfig, IndexConfig
from app.embeddings.errors import EmbeddingError, retryable
from app.embeddings.inputs import ChunkSource, build, eligible, encoded
from app.embeddings.medcpt import vector_checksum
from app.embeddings.model import EmbeddingVector
from app.embeddings.validation import (
    EMBEDDING_CHECKSUM_MISMATCH,
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_NON_FINITE,
    EMBEDDING_ZERO_VECTOR,
    QDRANT_COUNT_MISMATCH,
    QDRANT_PAYLOAD_MISMATCH,
    QDRANT_POINT_MISSING,
    QDRANT_POINT_UNEXPECTED,
    QDRANT_TENANT_MISMATCH,
    QDRANT_VECTOR_NAME_MISMATCH,
    ExpectedPoint,
    ObservedPoint,
    check_vector,
    reconcile,
    result,
)
from app.services.embedding import point_id
from pydantic import ValidationError

CACHE = Path(".local/models/embeddings")
MODEL = pytest.mark.skipif(
    not CACHE.exists(), reason="Run scripts/provision_embedding_model.py first"
)


def source(text: str = "Synthetic body text.", **overrides) -> ChunkSource:
    values = dict(
        chunk_id=uuid5(UUID(int=7), text),
        chunk_type="TEXT_CHILD",
        retrieval_text=text,
        document_title="Synthetic reference",
        hierarchy=("Chapter one", "Electrolytes"),
        caption=None,
    )
    values.update(overrides)
    return ChunkSource(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- policy


def test_configuration_pins_the_vector_semantics():
    config = EmbeddingConfig()
    assert config.model_id == "ncbi/MedCPT-Article-Encoder"
    assert config.model_revision == "d05a736da4bb84ee4057b7f7999485be6ed85465"
    assert config.embedding_dimension == 768
    assert config.pooling_strategy == "CLS"
    assert config.normalization == "NONE"
    assert config.distance_metric == "DOT"
    assert config.max_input_tokens == 512
    assert config.truncation_policy == "REJECT"
    assert len(config.model_checksum) == 64


def test_semantics_fingerprint_ignores_resources_but_not_representation():
    config = EmbeddingConfig()
    # Batching and threads change throughput, not meaning, so the vector space is unchanged.
    assert (
        config.model_copy(update={"batch_size": 64, "torch_threads": 8}).semantics_fingerprint
        == config.semantics_fingerprint
    )
    # The context representation is part of what a vector means.
    assert (
        config.model_copy(update={"max_context_characters": 80}).semantics_fingerprint
        != config.semantics_fingerprint
    )
    assert config.model_copy(update={"batch_size": 64}).fingerprint != config.fingerprint


def test_parent_chunks_cannot_be_declared_retrieval_eligible():
    with pytest.raises(ValidationError):
        EmbeddingConfig(eligible_chunk_types=("TEXT_CHILD", "TEXT_PARENT"))
    with pytest.raises(ValidationError):
        EmbeddingConfig(lease_seconds=60, timeout_seconds=120)
    with pytest.raises(ValidationError):
        IndexConfig(payload_indexes=("document_id",))


@pytest.mark.parametrize(
    "chunk_type,expected",
    [
        ("TEXT_CHILD", True),
        ("LIST", True),
        ("TABLE", True),
        ("TABLE_PART", True),
        ("FORMULA", True),
        ("FIGURE_CONTEXT", True),
        ("QUESTION", True),
        ("QUESTION_EXPLANATION", True),
        ("OTHER_STRUCTURED", True),
        # A parent is a context-expansion unit for a later milestone, never a first-stage result.
        ("TEXT_PARENT", False),
        ("UNKNOWN_KIND", False),
    ],
)
def test_retrieval_eligibility(chunk_type, expected):
    assert eligible(chunk_type, EmbeddingConfig()) is expected


# --------------------------------------------------------------------------- input builder


def test_input_is_two_fields_of_persisted_content_only():
    config = EmbeddingConfig()
    value = build(source("Sodium below 135 mmol/L."), config)
    context, body = encoded(value, config)
    assert context == "Synthetic reference > Chapter one > Electrolytes"
    # The body is the M3 retrieval representation verbatim: nothing summarized, nothing added.
    assert body == "Sodium below 135 mmol/L."


def test_input_hash_covers_everything_the_model_sees():
    config = EmbeddingConfig()
    base = build(source("Body."), config)
    assert build(source("Body."), config).input_hash == base.input_hash
    assert build(source("Body!"), config).input_hash != base.input_hash
    assert build(source("Body.", hierarchy=("Chapter two",)), config).input_hash != base.input_hash
    assert build(source("Body.", caption="Table 1."), config).input_hash != base.input_hash
    # A different model revision or builder version must not reuse an existing vector.
    other = config.model_copy(update={"version": "embedding-m4-v2"})
    assert build(source("Body."), other).input_hash == base.input_hash
    assert build(source("Body."), config.model_copy(update={"max_context_characters": 10}))


def test_context_is_trimmed_deterministically_and_keeps_the_nearest_heading():
    config = EmbeddingConfig(max_context_characters=40)
    value = build(source("Body.", hierarchy=("A" * 60, "Nearest heading")), config)
    assert value.context.startswith("...")
    assert value.context.endswith("Nearest heading")
    assert len(value.context) == 40
    assert build(source("Body.", hierarchy=("A" * 60, "Nearest heading")), config) == value


def test_empty_context_stays_an_empty_field_rather_than_moving_into_the_body():
    config = EmbeddingConfig()
    value = build(
        ChunkSource(
            chunk_id=uuid4(),
            chunk_type="TEXT_CHILD",
            retrieval_text="Body.",
            document_title="",
            hierarchy=(),
        ),
        config,
    )
    assert encoded(value, config) == ("", "Body.")


# --------------------------------------------------------------------------- identity


def test_point_identity_is_deterministic_and_vector_scoped():
    chunk = uuid4()
    assert point_id(chunk, "medcpt_dense") == point_id(chunk, "medcpt_dense")
    # A future sparse vector for the same chunk is a different point name, not a collision.
    assert point_id(chunk, "medcpt_dense") != point_id(chunk, "bm25_sparse")
    assert point_id(uuid4(), "medcpt_dense") != point_id(chunk, "medcpt_dense")


def test_vector_checksum_is_a_byte_identity():
    assert vector_checksum((0.1, 0.2)) == vector_checksum((0.1, 0.2))
    assert vector_checksum((0.1, 0.2)) != vector_checksum((0.2, 0.1))
    assert len(vector_checksum((0.1,))) == 64


# --------------------------------------------------------------------------- vector sanity


def vector(values, chunk=None) -> EmbeddingVector:
    numbers = tuple(values)
    return EmbeddingVector(
        chunk_id=chunk or uuid4(),
        values=numbers,
        token_count=10,
        checksum=vector_checksum(numbers),
        norm=math.sqrt(sum(value * value for value in numbers if math.isfinite(value))),
    )


def test_vector_sanity_rejects_malformed_representations():
    config = EmbeddingConfig()
    good = vector([0.0] * 767 + [1.0])
    assert check_vector(good, config) == []
    assert {f.code for f in check_vector(vector([0.1] * 10), config)} == {
        EMBEDDING_DIMENSION_MISMATCH
    }
    assert {f.code for f in check_vector(vector([float("nan")] * 768), config)} == {
        EMBEDDING_NON_FINITE
    }
    assert {f.code for f in check_vector(vector([float("inf")] * 768), config)} == {
        EMBEDDING_NON_FINITE
    }
    assert {f.code for f in check_vector(vector([0.0] * 768), config)} == {EMBEDDING_ZERO_VECTOR}


# --------------------------------------------------------------------------- reconciliation


def expected_point(chunk_id=None, values=(1.0, 2.0), tenant="t1", **payload):
    chunk_id = chunk_id or uuid4()
    return ExpectedPoint(
        point_id=point_id(chunk_id, "medcpt_dense"),
        chunk_id=chunk_id,
        vector_checksum=vector_checksum(tuple(values)),
        payload={
            "tenant_id": tenant,
            "document_id": "d1",
            "document_version_id": "v1",
            "chunk_run_id": "c1",
            "embedding_run_id": "e1",
            "chunk_id": str(chunk_id),
            "chunk_type": "TEXT_CHILD",
            **payload,
        },
    )


def observed(point: ExpectedPoint, values=(1.0, 2.0), **overrides):
    return ObservedPoint(
        point_id=point.point_id,
        vector=tuple(values) if values is not None else None,
        payload={**point.payload, **overrides},
    )


def config_for(dimension: int = 2) -> EmbeddingConfig:
    return EmbeddingConfig.model_construct(embedding_dimension=dimension)


def test_reconciliation_passes_only_on_exact_agreement():
    point = expected_point()
    outcome = reconcile(
        (point,), (observed(point),), (point.point_id,), config_for(), vector_checksum
    )
    assert outcome.ok and outcome.verified == 1 == outcome.expected == outcome.observed
    assert result(outcome.findings) == "PASS"


def test_reconciliation_detects_a_missing_point():
    point = expected_point()
    outcome = reconcile((point,), (), (), config_for(), vector_checksum)
    assert not outcome.ok
    assert QDRANT_POINT_MISSING in {f.code for f in outcome.findings}
    assert QDRANT_COUNT_MISMATCH in {f.code for f in outcome.findings}
    assert result(outcome.findings) == "FAIL"


def test_reconciliation_detects_an_unexpected_point():
    point = expected_point()
    stray = uuid4()
    outcome = reconcile(
        (point,), (observed(point),), (point.point_id, stray), config_for(), vector_checksum
    )
    assert not outcome.ok
    assert QDRANT_POINT_UNEXPECTED in {f.code for f in outcome.findings}


def test_reconciliation_detects_a_corrupted_vector():
    point = expected_point()
    outcome = reconcile(
        (point,),
        (observed(point, values=(9.0, 9.0)),),
        (point.point_id,),
        config_for(),
        vector_checksum,
    )
    assert not outcome.ok
    assert EMBEDDING_CHECKSUM_MISMATCH in {f.code for f in outcome.findings}


def test_reconciliation_detects_a_wrong_tenant_or_payload():
    point = expected_point()
    outcome = reconcile(
        (point,),
        (observed(point, tenant_id="other-tenant"),),
        (point.point_id,),
        config_for(),
        vector_checksum,
    )
    assert QDRANT_TENANT_MISMATCH in {f.code for f in outcome.findings}
    outcome = reconcile(
        (point,),
        (observed(point, chunk_run_id="another-dataset"),),
        (point.point_id,),
        config_for(),
        vector_checksum,
    )
    assert QDRANT_PAYLOAD_MISMATCH in {f.code for f in outcome.findings}


def test_reconciliation_detects_a_point_stored_under_the_wrong_vector_name():
    point = expected_point()
    outcome = reconcile(
        (point,), (observed(point, values=None),), (point.point_id,), config_for(), vector_checksum
    )
    assert QDRANT_VECTOR_NAME_MISMATCH in {f.code for f in outcome.findings}


def test_reconciliation_detects_a_wrong_dimension():
    point = expected_point()
    outcome = reconcile(
        (point,),
        (observed(point, values=(1.0, 2.0, 3.0)),),
        (point.point_id,),
        config_for(),
        vector_checksum,
    )
    assert EMBEDDING_DIMENSION_MISMATCH in {f.code for f in outcome.findings}


# --------------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "code,expected",
    [
        ("EMBEDDING_INFERENCE_FAILED", True),
        ("EMBEDDING_OOM", True),
        ("EMBEDDING_PERSISTENCE_FAILED", True),
        ("VECTOR_UPSERT_FAILED", True),
        ("VECTOR_INDEX_UNAVAILABLE", True),
        ("EMBEDDING_MODEL_LOAD_FAILED", True),
        # Deterministic incompatibilities: retrying them forever only hides the defect.
        ("EMBEDDING_INPUT_TOO_LONG", False),
        ("EMBEDDING_MODEL_REVISION_MISMATCH", False),
        ("EMBEDDING_MODEL_CHECKSUM_MISMATCH", False),
        ("VECTOR_SCHEMA_MISMATCH", False),
        ("VECTOR_RECONCILIATION_FAILED", False),
        ("EMBEDDING_NON_FINITE", False),
    ],
)
def test_error_retryability(code, expected):
    assert retryable(code) is expected
    assert EmbeddingError(code).details["retryable"] is expected


def test_unknown_codes_are_not_retryable():
    assert retryable("SOMETHING_ELSE") is False
    assert retryable(None) is False


# --------------------------------------------------------------------------- the real model


@pytest.fixture(scope="module")
def embedder():
    from app.embeddings.medcpt import MedCPTArticleEmbedder

    model = MedCPTArticleEmbedder(EmbeddingConfig(model_cache_dir=CACHE, offline=True))
    model.load()
    return model


@MODEL
def test_model_reports_the_pinned_specification(embedder):
    spec = embedder.specification
    config = EmbeddingConfig()
    assert spec.model_id == config.model_id
    assert spec.model_revision == config.model_revision
    assert spec.model_checksum == config.model_checksum
    assert spec.dimension == 768
    assert spec.pooling == "CLS"
    assert spec.normalization == "NONE"
    assert spec.distance_metric == "DOT"
    assert spec.max_input_tokens == 512
    assert spec.device == "cpu"
    assert set(spec.library_versions) >= {"transformers", "torch", "tokenizers"}


@MODEL
def test_tampered_checksum_fails_closed():
    from app.embeddings.medcpt import MedCPTArticleEmbedder

    config = EmbeddingConfig(model_cache_dir=CACHE, offline=True).model_copy(
        update={"model_checksum": "0" * 64}
    )
    with pytest.raises(EmbeddingError, match="EMBEDDING_MODEL_CHECKSUM_MISMATCH"):
        MedCPTArticleEmbedder(config).load()


@MODEL
def test_missing_model_offline_fails_closed(tmp_path):
    from app.embeddings.medcpt import MedCPTArticleEmbedder

    config = EmbeddingConfig(model_cache_dir=tmp_path, offline=True)
    with pytest.raises(EmbeddingError, match="EMBEDDING_MODEL_UNAVAILABLE_OFFLINE"):
        MedCPTArticleEmbedder(config).load()


@MODEL
def test_vectors_have_the_expected_shape_and_are_finite(embedder):
    config = EmbeddingConfig()
    values = embedder.embed_documents((build(source("Synthetic clinical body text."), config),))
    assert len(values) == 1
    assert len(values[0].values) == 768
    assert all(math.isfinite(number) for number in values[0].values)
    assert values[0].norm > 0
    assert check_vector(values[0], config) == []


@MODEL
def test_repeated_inference_on_one_host_is_byte_identical(embedder):
    value = build(source("Repeatability is measured, not assumed."), EmbeddingConfig())
    first = embedder.embed_documents((value,))[0]
    second = embedder.embed_documents((value,))[0]
    assert first.checksum == second.checksum
    assert first.values == second.values


@MODEL
def test_batching_agrees_with_single_inference_within_float_tolerance(embedder):
    """Batching changes padding, so float arithmetic differs in the last bits.

    The vectors are numerically equivalent — cosine agreement to 1.0 within 1e-6 — but they are
    *not* byte identical, so a checksum is a storage-integrity identity rather than a promise of
    reproducibility across batch layouts. This is measured here rather than assumed either way.
    """
    config = EmbeddingConfig()
    texts = ["First synthetic passage.", "Second, rather longer, synthetic passage about sodium."]
    inputs = tuple(build(source(text), config) for text in texts)
    batched = embedder.embed_documents(inputs)
    singles = [embedder.embed_documents((value,))[0] for value in inputs]
    for grouped, alone in zip(batched, singles, strict=True):
        delta = max(abs(a - b) for a, b in zip(grouped.values, alone.values, strict=True))
        assert delta < 1e-4
        dot = sum(a * b for a, b in zip(grouped.values, alone.values, strict=True))
        assert abs(dot / (grouped.norm * alone.norm) - 1.0) < 1e-6


@MODEL
def test_input_over_the_model_limit_is_rejected_never_truncated(embedder):
    config = EmbeddingConfig()
    huge = build(source("sodium potassium chloride bicarbonate " * 200), config)
    measured = embedder.measure((huge,))[0]
    assert measured.token_count > config.max_input_tokens
    assert measured.exceeds_limit
    with pytest.raises(EmbeddingError, match="EMBEDDING_INPUT_TOO_LONG") as raised:
        embedder.embed_documents((huge,))
    assert str(huge.chunk_id) in raised.value.details["chunk_ids"]
    assert raised.value.details["retryable"] is False


@MODEL
def test_a_paraphrase_scores_above_unrelated_text(embedder):
    """A coarse sanity check that the encoder is not catastrophically broken.

    This is not retrieval accuracy and nothing is tuned against it. It asserts only that a
    near-duplicate of a passage scores above obviously unrelated prose under the configured
    inner-product metric.
    """
    config = EmbeddingConfig()
    passages = {
        "anchor": "Serum sodium below 135 mmol per litre defines hyponatremia in this fixture.",
        "near": "Hyponatremia is defined in this fixture as serum sodium under 135 mmol per litre.",
        "far": "The synthetic timetable lists locomotive departures from the northern platform.",
        "far2": "A synthetic bread recipe needs flour, water, salt and time to rise before baking.",
    }
    vectors = {
        name: embedder.embed_documents((build(source(text), config),))[0].values
        for name, text in passages.items()
    }

    def score(a: str, b: str) -> float:
        return sum(x * y for x, y in zip(vectors[a], vectors[b], strict=True))

    assert score("anchor", "near") > score("anchor", "far")
    assert score("anchor", "near") > score("anchor", "far2")
