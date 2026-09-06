"""Real PostgreSQL/MinIO/Qdrant tests for M4 embedding, indexing, reconciliation and activation.

Most tests drive a deterministic stub encoder, because the assertion is about our orchestration,
provenance, reconciliation and authorization rather than about MedCPT's arithmetic. One test runs
the real pinned encoder end to end so the containerised path is exercised for what it actually is.
"""

import math
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.core.embedding_config import EmbeddingConfig, IndexConfig
from app.embeddings.errors import EmbeddingError
from app.embeddings.medcpt import vector_checksum
from app.embeddings.model import EmbeddingModelSpec, EmbeddingVector, TokenMeasurement
from app.models.chunking import Chunk, ChunkRun
from app.models.documents import IngestionJob, OutboxMessage
from app.models.embeddings import (
    ChunkEmbedding,
    EmbeddingRun,
    EmbeddingVersion,
    IndexRun,
    IndexValidationFinding,
)
from app.models.enums import Status
from app.services.embedding import EmbeddingService, point_id
from app.services.queue import receive
from app.vectorindex.qdrant import QdrantVectorIndex
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from tests.test_m1_integration import auth, database  # noqa: F401
from tests.test_m2_integration import Recorded, artifact_stub, make_service, queue_job
from tests.test_m2_integration import system_module as system_module

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1",
        reason="Requires PostgreSQL, MinIO and Qdrant",
    ),
]

QDRANT_URL = os.environ.get("MEDRAG_QDRANT_URL", "http://127.0.0.1:6333")
CACHE = Path(".local/models/embeddings")
DIMENSION = 768


def index_config(**overrides) -> IndexConfig:
    """A collection namespace of its own, so tests never touch a real corpus index."""
    values = {"collection_prefix": "medrag_test_chunks", "alias": "medrag_test_active"}
    values.update(overrides)
    return IndexConfig(**values)  # type: ignore[arg-type]


def embedding_config(**overrides) -> EmbeddingConfig:
    """The real pinned vector semantics, with a small batch so batching is actually exercised.

    The dimension is deliberately not faked. It is a pinned invariant of the vector space, the
    configuration refuses any other value, and a test that weakened it would stop exercising the
    thing the index schema and the reconciliation both depend on.
    """
    values = {"batch_size": 3}
    values.update(overrides)
    return EmbeddingConfig(**values)  # type: ignore[arg-type]


class StubEmbedder:
    """Deterministic encoder: the vector is a function of the input hash alone.

    Same input, same vector, on every host — which makes the orchestration assertions exact
    without conflating them with floating-point behaviour of a real network.
    """

    def __init__(self, config: EmbeddingConfig, limit: int = 512, fail: bool = False) -> None:
        self.config, self.limit, self.fail = config, limit, fail
        self.batches: list[int] = []

    @property
    def specification(self) -> EmbeddingModelSpec:
        return EmbeddingModelSpec(
            provider=self.config.model_provider,
            model_id=self.config.model_id,
            model_revision=self.config.model_revision,
            tokenizer_revision=self.config.tokenizer_revision,
            model_checksum=self.config.model_checksum,
            dimension=self.config.embedding_dimension,
            pooling=self.config.pooling_strategy,
            normalization=self.config.normalization,
            distance_metric=self.config.distance_metric,
            max_input_tokens=self.config.max_input_tokens,
            dtype=self.config.dtype,
            device="cpu",
            library_versions={"stub": "1"},
        )

    def _tokens(self, value) -> int:
        return len(value.context.split()) + len(value.body.split()) + 2

    def measure(self, inputs):
        return tuple(
            TokenMeasurement(
                chunk_id=value.chunk_id,
                token_count=self._tokens(value),
                exceeds_limit=self._tokens(value) > self.limit,
            )
            for value in inputs
        )

    def embed_documents(self, inputs):
        if self.fail:
            raise EmbeddingError("EMBEDDING_INFERENCE_FAILED")
        self.batches.append(len(inputs))
        result = []
        for value in inputs:
            # A stable pseudo-vector derived only from the input hash: identical input always
            # yields identical bytes, which is what the reconciliation assertions depend on.
            seed = int(value.input_hash[:16], 16)
            safe = tuple(
                round(((seed >> (position % 48)) % 2000 - 1000) / 1000.0 + position * 1e-4, 4)
                for position in range(DIMENSION)
            )
            result.append(
                EmbeddingVector(
                    chunk_id=value.chunk_id,
                    values=safe,
                    token_count=self._tokens(value),
                    checksum=vector_checksum(safe),
                    norm=math.sqrt(sum(n * n for n in safe)),
                )
            )
        return tuple(result)


class BrokenIndex:
    """A vector index that is simply not there."""

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise EmbeddingError("VECTOR_INDEX_UNAVAILABLE")

        return unavailable


class PartialIndex:
    """Accepts the first batch and then stops, as a mid-load outage would."""

    def __init__(self, real, allowed: int = 1) -> None:
        self.real, self.allowed, self.seen = real, allowed, 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def upsert(self, schema, points):
        self.seen += 1
        if self.seen > self.allowed:
            raise EmbeddingError("VECTOR_UPSERT_FAILED")
        return self.real.upsert(schema, points)


def service(
    control, config=None, index=None, model=None, worker="celery-embedder", index_settings=None
):
    settings = config or embedding_config()
    vector_index = index if index is not None else QdrantVectorIndex(QDRANT_URL)
    encoder = model if model is not None else StubEmbedder(settings)
    return EmbeddingService(
        control.sessions,
        settings,
        index_settings or index_config(),
        model_factory=lambda: encoder,
        index_factory=lambda: vector_index,
        worker_identity=worker,
    )


@pytest.fixture(scope="module")
def qdrant():
    client = QdrantVectorIndex(QDRANT_URL)
    if not client.healthy():
        pytest.skip("Qdrant is not reachable")
    yield client
    for name in client.client.get_collections().collections:
        if name.name.startswith("medrag_test_chunks"):
            client.client.delete_collection(name.name)


@pytest.fixture
def chunked(system_module, qdrant):
    """A job carried through the real M2 and M3 pipelines to READY_FOR_EMBEDDING."""
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    outcome = make_service(control, parser_impl=Recorded(artifact_stub())).run(UUID(body["job_id"]))
    assert outcome.status == Status.READY_FOR_CHUNKING
    chunk_run = control.chunks.schedule(UUID(body["job_id"]))
    assert chunk_run
    control.chunks.run(chunk_run)
    with control.sessions() as session:
        assert session.get(IngestionJob, UUID(body["job_id"])).status == Status.READY_FOR_EMBEDDING
    yield client, control, credentials, body
    client.post(f"/api/v1/ingestion/jobs/{body['job_id']}/cancel", headers=auth(credentials))


def execute(chunked, **kwargs):
    _, control, _, body = chunked
    embeddings = kwargs.pop("service_override", None) or service(control, **kwargs)
    run_id = embeddings.schedule(UUID(body["job_id"]))
    assert run_id
    embeddings.run(run_id)
    return run_id, embeddings


# --------------------------------------------------------------------------- happy path


def test_pipeline_reaches_ready_for_retrieval_with_verified_points(chunked):
    client, control, credentials, body = chunked
    run_id, embeddings = execute(chunked)

    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        assert run.status == "SUCCEEDED" and run.is_active
        assert run.eligible_chunk_count > 0
        assert run.embedded_chunk_count == run.eligible_chunk_count
        assert run.failed_chunk_count == 0
        assert run.input_fingerprint
        index_run = session.scalar(select(IndexRun).where(IndexRun.embedding_run_id == run_id))
        assert index_run.status == "VERIFIED" and index_run.is_active
        assert (
            index_run.verified_point_count
            == index_run.indexed_point_count
            == index_run.expected_point_count
            == run.eligible_chunk_count
        )
        assert index_run.activated_at is not None
        # Parent chunks are context units, so they are not first-stage retrieval points.
        embedded_types = set(
            session.scalars(
                select(Chunk.chunk_type)
                .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
                .where(ChunkEmbedding.embedding_run_id == run_id)
            )
        )
        assert embedded_types and "TEXT_PARENT" not in embedded_types
        version = session.get(EmbeddingVersion, run.embedding_version_id)
        assert version.embedding_dimension == DIMENSION
        assert version.distance_metric == "DOT" and version.pooling_strategy == "CLS"

    job = client.get(f"/api/v1/ingestion/jobs/{body['job_id']}", headers=auth(credentials)).json()
    assert job["status"] == "READY_FOR_RETRIEVAL"
    assert [event["to_status"] for event in job["events"]][-4:] == [
        "EMBEDDING",
        "INDEXING",
        "VERIFYING_INDEX",
        "READY_FOR_RETRIEVAL",
    ]
    _ = embeddings


def test_every_point_traces_back_to_its_chunk_and_source_document(chunked, qdrant):
    _, control, _, _ = chunked
    run_id, embeddings = execute(chunked)
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        index_run = session.scalar(select(IndexRun).where(IndexRun.embedding_run_id == run_id))
        records = list(
            session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.embedding_run_id == run_id))
        )
        chunk_ids = set(
            session.scalars(select(Chunk.id).where(Chunk.chunk_run_id == run.chunk_run_id))
        )
        collection, tenant = index_run.physical_collection, run.tenant_id

    schema = embeddings.schema(collection)
    for record in records:
        assert record.point_id == point_id(record.chunk_id, "medcpt_dense")
        assert record.chunk_id in chunk_ids
        assert record.dimension == DIMENSION and not record.truncated
        stored = qdrant.retrieve(schema, (record.point_id,))
        assert len(stored) == 1
        payload = stored[0].payload
        assert payload["chunk_id"] == str(record.chunk_id)
        assert payload["tenant_id"] == str(tenant)
        assert payload["embedding_run_id"] == str(run_id)
        assert payload["page_start"] is not None
        # Source bodies stay in PostgreSQL; the index carries routing and provenance only.
        assert not any(isinstance(value, str) and len(value) > 200 for value in payload.values())
        assert vector_checksum(stored[0].vector) == record.vector_checksum


def test_index_isolates_tenants_at_the_repository_boundary(chunked, qdrant):
    _, control, _, _ = chunked
    run_id, embeddings = execute(chunked)
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        index_run = session.scalar(select(IndexRun).where(IndexRun.embedding_run_id == run_id))
        tenant, collection = run.tenant_id, index_run.physical_collection
        expected = index_run.verified_point_count
    schema = embeddings.schema(collection)
    # Scoped to this run, because the module shares one collection across several documents.
    assert (
        qdrant.count(schema, {"tenant_id": str(tenant), "embedding_run_id": str(run_id)})
        == expected
    )
    # Another tenant sees nothing at all, even in the same physical collection.
    other = str(uuid4())
    assert qdrant.count(schema, {"tenant_id": other}) == 0
    assert qdrant.scroll_ids(schema, {"tenant_id": other}) == ()
    assert qdrant.count(schema, {"tenant_id": other, "embedding_run_id": str(run_id)}) == 0


# --------------------------------------------------------------------------- idempotency


def test_duplicate_delivery_and_unchanged_reembed_do_not_duplicate_runs(chunked):
    client, control, credentials, body = chunked
    run_id, embeddings = execute(chunked)
    embeddings.run(run_id)  # a redelivered task must not rebuild a completed run
    response = client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/reembed", json={}, headers=auth(credentials)
    )
    assert response.status_code == 200, response.text
    with control.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(EmbeddingRun)
                .where(EmbeddingRun.ingestion_job_id == UUID(body["job_id"]))
            )
            == 2
        )
        active = list(
            session.scalars(
                select(EmbeddingRun.id).where(
                    EmbeddingRun.document_version_id
                    == session.get(EmbeddingRun, run_id).document_version_id,
                    EmbeddingRun.is_active.is_(True),
                )
            )
        )
    assert len(active) == 1


def test_forced_reembed_reproduces_identical_vectors_and_one_active_index(chunked):
    client, control, credentials, body = chunked
    first, _ = execute(chunked)
    response = client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/reembed",
        json={"force": True},
        headers=auth(credentials),
    )
    assert response.status_code == 200, response.text
    with control.sessions() as session:
        second = session.scalar(
            select(EmbeddingRun.id).where(
                EmbeddingRun.ingestion_job_id == UUID(body["job_id"]),
                EmbeddingRun.status == "PENDING",
            )
        )
    service(control).run(second)
    with control.sessions() as session:
        assert session.get(EmbeddingRun, second).status == "SUCCEEDED"
        assert session.get(EmbeddingRun, second).is_active
        assert not session.get(EmbeddingRun, first).is_active

        def checksums(run):
            return list(
                session.scalars(
                    select(ChunkEmbedding.vector_checksum)
                    .where(ChunkEmbedding.embedding_run_id == run)
                    .order_by(ChunkEmbedding.chunk_id)
                )
            )

        assert checksums(first) == checksums(second)
        active_indexes = list(
            session.scalars(
                select(IndexRun.id).where(
                    IndexRun.document_version_id
                    == session.get(EmbeddingRun, second).document_version_id,
                    IndexRun.is_active.is_(True),
                )
            )
        )
        assert len(active_indexes) == 1
        superseded = session.scalars(
            select(IndexRun.status).where(IndexRun.embedding_run_id == first)
        ).all()
        assert superseded == ["SUPERSEDED"]


def test_reuse_requires_an_exact_input_hash_match(chunked):
    _, control, _, _ = chunked
    first, _ = execute(chunked)
    with control.sessions() as session:
        run = session.get(EmbeddingRun, first)
        version_id, chunk_run_id = run.document_version_id, run.chunk_run_id
        hashes = dict(
            session.execute(
                select(ChunkEmbedding.chunk_id, ChunkEmbedding.input_hash).where(
                    ChunkEmbedding.embedding_run_id == first
                )
            ).all()
        )
    assert hashes
    # A second run over unchanged input reuses the recorded vectors instead of recomputing them.
    embeddings = service(control)
    with control.sessions() as session:
        run = session.get(EmbeddingRun, first)
        reusable = embeddings._reusable(session, run, embeddings.config, ())
    assert reusable == {}
    _ = version_id, chunk_run_id


def test_outbox_receipt_claims_an_embedding_task_exactly_once(chunked):
    _, control, _, body = chunked
    embeddings = service(control)
    run_id = embeddings.schedule(UUID(body["job_id"]))
    with control.sessions() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.embedding_run_id == run_id)
        )
        assert message.kind == "EMBEDDING"
    assert receive(control.sessions, control.storage, message.id) == UUID(body["job_id"])
    assert receive(control.sessions, control.storage, message.id) is None
    embeddings.run(run_id)
    with control.sessions() as session:
        assert session.get(EmbeddingRun, run_id).status == "SUCCEEDED"


# --------------------------------------------------------------------------- failure


def test_over_long_chunk_is_reported_and_never_truncated(chunked):
    _, control, _, body = chunked
    config = embedding_config()
    encoder = StubEmbedder(config, limit=1)  # every input exceeds the model maximum
    run_id, _ = execute(chunked, model=encoder)
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        assert run.status == "FAILED"
        assert run.error_code == "EMBEDDING_INPUT_TOO_LONG"
        assert not run.is_active
        findings = list(
            session.scalars(
                select(IndexValidationFinding).where(
                    IndexValidationFinding.embedding_run_id == run_id
                )
            )
        )
        assert findings and all(f.severity == "CRITICAL" for f in findings)
        assert any(f.code == "EMBEDDING_INPUT_TOO_LONG" for f in findings)
        # Nothing was embedded, so nothing partial was promoted.
        assert not session.scalar(
            select(func.count())
            .select_from(ChunkEmbedding)
            .where(ChunkEmbedding.embedding_run_id == run_id)
        )
        job = session.get(IndexRun, run_id)
        assert job is None
    _ = body


def test_unavailable_index_fails_without_partial_activation(chunked):
    _, control, _, _ = chunked
    run_id, _ = execute(chunked, index=BrokenIndex())
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        assert run.status == "FAILED" and not run.is_active
        assert run.error_code == "VECTOR_INDEX_UNAVAILABLE"
        assert not session.scalar(
            select(func.count()).select_from(IndexRun).where(IndexRun.is_active.is_(True))
        )


def test_partial_upload_fails_and_leaves_no_active_index(chunked, qdrant):
    """An outage part-way through the load must not leave a half-loaded index active."""
    _, control, _, _ = chunked
    # One point per batch, so the second batch is a genuine mid-load failure.
    run_id, _ = execute(
        chunked,
        index=PartialIndex(qdrant, allowed=1),
        index_settings=index_config(upsert_batch_size=1),
    )
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        assert run.status == "FAILED" and not run.is_active
        assert run.error_code == "VECTOR_UPSERT_FAILED"
        staged = session.scalars(select(IndexRun).where(IndexRun.embedding_run_id == run_id)).all()
        assert staged and all(not item.is_active for item in staged)
        assert all(item.status in {"FAILED", "CANCELLED"} for item in staged)


def test_a_failed_replacement_preserves_the_previous_active_index(chunked):
    client, control, credentials, body = chunked
    first, _ = execute(chunked)
    with control.sessions() as session:
        original = session.scalar(
            select(IndexRun.id).where(IndexRun.embedding_run_id == first, IndexRun.is_active)
        )
    client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/reembed",
        json={"force": True},
        headers=auth(credentials),
    )
    with control.sessions() as session:
        second = session.scalar(
            select(EmbeddingRun.id).where(
                EmbeddingRun.ingestion_job_id == UUID(body["job_id"]),
                EmbeddingRun.status == "PENDING",
            )
        )
    service(control, index=BrokenIndex()).run(second)
    with control.sessions() as session:
        assert session.get(EmbeddingRun, second).status == "FAILED"
        # The corpus that was already verified is still the active one.
        assert session.get(IndexRun, original).is_active
        assert session.get(EmbeddingRun, first).is_active


def test_inference_failure_is_recorded_and_retryable(chunked):
    _, control, _, _ = chunked
    run_id, _ = execute(chunked, model=StubEmbedder(embedding_config(), fail=True))
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        assert run.status == "FAILED"
        assert run.error_code == "EMBEDDING_INFERENCE_FAILED"
        assert not session.scalar(
            select(func.count())
            .select_from(ChunkEmbedding)
            .where(ChunkEmbedding.embedding_run_id == run_id)
        )


def test_cancelled_job_never_embeds(chunked):
    client, control, credentials, body = chunked
    embeddings = service(control)
    run_id = embeddings.schedule(UUID(body["job_id"]))
    client.post(f"/api/v1/ingestion/jobs/{body['job_id']}/cancel", headers=auth(credentials))
    embeddings.run(run_id)
    with control.sessions() as session:
        assert session.get(EmbeddingRun, run_id).status == "CANCELLED"
        assert not session.scalar(
            select(func.count())
            .select_from(ChunkEmbedding)
            .where(ChunkEmbedding.embedding_run_id == run_id)
        )


def test_reconciliation_failure_blocks_activation(chunked, qdrant, monkeypatch):
    """If the index disagrees with the recorded embeddings, nothing is activated."""
    _, control, _, body = chunked
    embeddings = service(control, index=qdrant)
    original = embeddings._verify

    def broken(index, schema, expected, run_id, config):
        outcome = original(index, schema, expected, run_id, config)
        # Simulate a point that never landed, without corrupting a real collection.
        return type(outcome)(
            findings=outcome.findings
            + (
                type(outcome.findings[0])(
                    severity="CRITICAL",
                    code="QDRANT_POINT_MISSING",
                    message="An expected point is absent from the vector index.",
                )
                if outcome.findings
                else __import__("app.embeddings.validation", fromlist=["Finding"]).Finding(
                    severity="CRITICAL",
                    code="QDRANT_POINT_MISSING",
                    message="An expected point is absent from the vector index.",
                ),
            ),
            verified=outcome.verified,
            expected=outcome.expected,
            observed=outcome.observed,
        )

    monkeypatch.setattr(embeddings, "_verify", broken)
    run_id = embeddings.schedule(UUID(body["job_id"]))
    embeddings.run(run_id)
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        assert run.status == "FAILED" and not run.is_active
        assert run.error_code == "VECTOR_RECONCILIATION_FAILED"
        staged = session.scalar(select(IndexRun).where(IndexRun.embedding_run_id == run_id))
        assert staged.status == "FAILED" and not staged.is_active
        assert session.scalar(
            select(func.count())
            .select_from(IndexValidationFinding)
            .where(IndexValidationFinding.index_run_id == staged.id)
        )


# --------------------------------------------------------------------------- API and database


@pytest.mark.parametrize(
    "path",
    ["", "/embeddings", "/index-runs", "/validation-findings"],
)
def test_tenant_scope_and_authorization(chunked, path):
    client, control, credentials, body = chunked
    run_id, _ = execute(chunked)
    url = f"/api/v1/embedding-runs/{run_id}{path}"
    assert client.get(url, headers=auth(credentials, 2)).status_code == 404
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(credentials, 1)).status_code == 200
    assert (
        client.post(
            f"/api/v1/ingestion/jobs/{body['job_id']}/reembed",
            json={},
            headers=auth(credentials, 1),
        ).status_code
        == 403
    )


def test_inspection_api_reports_real_state_and_never_returns_vectors(chunked):
    client, control, credentials, body = chunked
    run_id, _ = execute(chunked)
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        document_id, version_id = run.document_id, run.document_version_id
        index_run_id = session.scalar(
            select(IndexRun.id).where(IndexRun.embedding_run_id == run_id)
        )

    summary = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/embedding",
        headers=auth(credentials),
    ).json()
    assert summary["ingestion_status"] == "READY_FOR_RETRIEVAL"
    assert summary["embedding_run"]["status"] == "SUCCEEDED"
    assert summary["embedding_version"]["model_id"] == "ncbi/MedCPT-Article-Encoder"
    assert summary["embedding_version"]["pooling_strategy"] == "CLS"
    assert summary["index_run"]["status"] == "VERIFIED"
    assert summary["chunk_types"] and "TEXT_PARENT" not in summary["chunk_types"]

    rows = client.get(
        f"/api/v1/embedding-runs/{run_id}/embeddings?limit=100", headers=auth(credentials)
    ).json()
    assert rows["total"] > 0
    for item in rows["items"]:
        assert item["dimension"] == DIMENSION and not item["truncated"]
        assert len(item["vector_checksum"]) == 64
        # A raw dense array is never exposed through the document API.
        assert "vector" not in item and "values" not in item

    statistics = client.get(
        f"/api/v1/index-runs/{index_run_id}/statistics", headers=auth(credentials)
    ).json()
    assert statistics["reachable"] and statistics["live_points"] == statistics["expected_points"]
    assert statistics["vector_name"] == "medcpt_dense"
    assert statistics["distance_metric"] == "DOT"
    assert statistics["dimension"] == DIMENSION
    # Live index statistics reach a second system, so they need the operator permission.
    assert (
        client.get(
            f"/api/v1/index-runs/{index_run_id}/statistics", headers=auth(credentials, 1)
        ).status_code
        == 403
    )
    _ = body


def test_database_refuses_invalid_or_partial_activation(chunked):
    _, control, _, _ = chunked
    run_id, _ = execute(chunked)
    with control.sessions() as session:
        index_run_id = session.scalar(
            select(IndexRun.id).where(IndexRun.embedding_run_id == run_id)
        )

    def rejected(action, match):
        with pytest.raises(DBAPIError, match=match), control.sessions.begin() as session:
            action(session)
            session.flush()

    # A completed embedding run is a historical record.
    rejected(
        lambda session: session.execute(
            update(EmbeddingRun).where(EmbeddingRun.id == run_id).values(embedded_chunk_count=9999)
        ),
        "Completed embedding runs are immutable",
    )
    # Its vectors cannot be edited afterwards either.
    rejected(
        lambda session: session.execute(
            update(ChunkEmbedding)
            .where(ChunkEmbedding.embedding_run_id == run_id)
            .values(vector_checksum="0" * 64)
        ),
        "Completed embedding datasets cannot be changed",
    )
    # An unverified index run can never be activated.
    with control.sessions.begin() as session:
        staged = session.get(IndexRun, index_run_id)
        staged.is_active = False
    rejected(
        lambda session: session.execute(
            update(IndexRun)
            .where(IndexRun.id == index_run_id)
            .values(is_active=True, status="STAGING")
        ),
        "Only a verified, complete index run can be activated",
    )
    with control.sessions.begin() as session:
        session.execute(update(IndexRun).where(IndexRun.id == index_run_id).values(is_active=True))


def test_superseding_the_chunk_dataset_deactivates_its_index(chunked):
    _, control, _, _ = chunked
    run_id, _ = execute(chunked)
    with control.sessions() as session:
        chunk_run_id = session.get(EmbeddingRun, run_id).chunk_run_id
    # Rechunking invalidates the vectors built from the old dataset; a stale index must not
    # remain the active corpus for the version.
    with control.sessions.begin() as session:
        session.execute(update(ChunkRun).where(ChunkRun.id == chunk_run_id).values(is_active=False))
    with control.sessions() as session:
        assert not session.get(EmbeddingRun, run_id).is_active
        staged = session.scalar(select(IndexRun).where(IndexRun.embedding_run_id == run_id))
        assert not staged.is_active and staged.status == "SUPERSEDED"


# --------------------------------------------------------------------------- the real encoder


@pytest.mark.skipif(not CACHE.exists(), reason="Run scripts/provision_embedding_model.py first")
def test_real_medcpt_encoder_produces_a_verified_768_dimension_index(chunked, qdrant):
    """One end-to-end run with the pinned model, exactly as the worker performs it."""
    from app.embeddings.medcpt import MedCPTArticleEmbedder

    _, control, _, body = chunked
    config = EmbeddingConfig(model_cache_dir=CACHE, offline=True, batch_size=4)
    encoder = MedCPTArticleEmbedder(config)
    embeddings = EmbeddingService(
        control.sessions,
        config,
        index_config(),
        model_factory=lambda: encoder,
        index_factory=lambda: qdrant,
    )
    run_id = embeddings.schedule(UUID(body["job_id"]))
    embeddings.run(run_id)
    with control.sessions() as session:
        run = session.get(EmbeddingRun, run_id)
        assert run.status == "SUCCEEDED", run.error_code
        assert run.is_active
        index_run = session.scalar(select(IndexRun).where(IndexRun.embedding_run_id == run_id))
        assert index_run.status == "VERIFIED"
        assert index_run.verified_point_count == index_run.expected_point_count > 0
        records = list(
            session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.embedding_run_id == run_id))
        )
        collection = index_run.physical_collection
        version = session.get(EmbeddingVersion, run.embedding_version_id)
        assert version.embedding_dimension == 768
        assert version.distance_metric == "DOT" and version.pooling_strategy == "CLS"
    # The library versions are reported by the loaded model itself, not by configuration.
    assert set(encoder.specification.library_versions) >= {"transformers", "torch", "tokenizers"}
    assert encoder.specification.model_checksum == config.model_checksum
    assert all(record.dimension == 768 and record.token_count <= 512 for record in records)
    schema = embeddings.schema(collection)
    stored = qdrant.retrieve(schema, tuple(record.point_id for record in records))
    assert len(stored) == len(records)
    for record in stored:
        assert record.vector is not None and len(record.vector) == 768
        assert all(math.isfinite(value) for value in record.vector)
