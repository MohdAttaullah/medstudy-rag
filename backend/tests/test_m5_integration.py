# ruff: noqa: F811
"""Real PostgreSQL/MinIO/Qdrant tests for M5 lexical indexing, hybrid retrieval and isolation.

Most tests drive deterministic stub encoders, because the assertions are about corpus resolution,
version alignment, tenancy, reconciliation and authorization rather than about MedCPT's
arithmetic. Two tests run both real pinned encoders end to end so the path the deployment actually
uses is exercised for what it is.

The stub query encoder returns the vector of a chosen chunk verbatim. That makes the dense lane's
answer known in advance, so a failure here is a failure of filtering, scoping or plumbing rather
than an unexplained change in a similarity score.
"""

import os
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from app.core.errors import DomainError
from app.core.retrieval_config import (
    QueryEncoderConfig,
    RetrievalConfig,
    SparseAnalyzerConfig,
    SparseIndexConfig,
)
from app.models.chunking import Chunk, ChunkRun
from app.models.documents import IngestionJob, OutboxMessage
from app.models.embeddings import ChunkEmbedding, IndexRun
from app.models.enums import Status
from app.models.retrieval import (
    QueryEncoderVersion,
    SparseDocument,
    SparseIndex,
    SparseIndexVersion,
    SparsePosting,
    SparseTerm,
    SparseValidationFinding,
)
from app.observability.retrieval import RetrievalMetrics
from app.repositories.retrieval import PostgresSparseRetriever, hydrate, resolve_corpus
from app.retrieval.errors import RetrievalError
from app.retrieval.model import QueryEncoderSpec, QueryVector, RetrievalFilters
from app.security.auth import Principal
from app.services.queue import receive
from app.services.retrieval import RetrievalService
from app.services.sparse_index import SparseIndexService
from prometheus_client import CollectorRegistry
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from tests.test_m1_integration import auth, database  # noqa: F401
from tests.test_m2_integration import system_module as system_module
from tests.test_m4_integration import (  # noqa: F401
    CACHE,
    DIMENSION,
    StubEmbedder,
    chunked,
    embedding_config,
    index_config,
    qdrant,
    service,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1",
        reason="Requires PostgreSQL, MinIO and Qdrant",
    ),
]

ANALYZER = SparseAnalyzerConfig()


def sparse_config(**overrides) -> SparseIndexConfig:
    values = {"posting_batch_size": 50}
    values.update(overrides)
    return SparseIndexConfig(**values)  # type: ignore[arg-type]


class StubQueryEncoder:
    """Returns a fixed vector and reports the pinned specification.

    It exists so a dense ranking is knowable in advance. It never claims to be MedCPT beyond the
    identity fields the service checks, and the two tests that need real query semantics load the
    real encoder instead.
    """

    def __init__(self, config: QueryEncoderConfig, vector: tuple[float, ...] | None = None) -> None:
        self.config = config
        self.vector = vector or tuple(0.0 for _ in range(DIMENSION))
        self.calls = 0

    @property
    def specification(self) -> QueryEncoderSpec:
        return QueryEncoderSpec(
            provider=self.config.model_provider,
            model_id=self.config.model_id,
            model_revision=self.config.model_revision,
            tokenizer_revision=self.config.tokenizer_revision,
            model_checksum=self.config.model_checksum,
            tokenizer_checksum=self.config.tokenizer_checksum,
            dimension=self.config.embedding_dimension,
            pooling=self.config.pooling_strategy,
            normalization=self.config.normalization,
            distance_metric=self.config.distance_metric,
            max_query_tokens=self.config.max_query_tokens,
            dtype=self.config.dtype,
            device="cpu",
            normalization_version=self.config.normalization_version,
            semantics_fingerprint=self.config.semantics_fingerprint,
            library_versions={"stub": "1"},
        )

    def measure(self, queries):
        return ()

    def encode_queries(self, queries):
        self.calls += 1
        return tuple(
            QueryVector(
                values=self.vector,
                token_count=len(text.split()) + 2,
                normalized=text,
                query_hash=f"stub-{abs(hash(text))}",
                checksum="stub",
                norm=1.0,
            )
            for text in queries
        )


class DivergentEncoder(StubQueryEncoder):
    """Reports a revision that is not the pinned one."""

    @property
    def specification(self) -> QueryEncoderSpec:
        base = super().specification
        return replace(base, model_revision="0" * 40)


def sparse_service(control, analyzer=None, config=None, worker="celery-sparse-indexer"):
    return SparseIndexService(
        control.sessions, analyzer or ANALYZER, config or sparse_config(), worker_identity=worker
    )


def retrieval_service(control, encoder, index, config=None, analyzer=None):
    return RetrievalService(
        control.sessions,
        QueryEncoderConfig(),
        analyzer or ANALYZER,
        config or RetrievalConfig(),
        encoder_factory=lambda: encoder,
        index_factory=lambda: index,
        metrics=RetrievalMetrics(CollectorRegistry()),
    )


@pytest.fixture
def indexed(chunked, qdrant):
    """A job carried through the real M2, M3 and M4 pipelines and then the M5 lexical build."""
    client, control, credentials, body = chunked
    embeddings = service(control, index=qdrant)
    run_id = embeddings.schedule(UUID(body["job_id"]))
    assert run_id
    embeddings.run(run_id)
    with control.sessions() as session:
        assert session.get(IngestionJob, UUID(body["job_id"])).status == Status.READY_FOR_RETRIEVAL
    return client, control, credentials, body, run_id, embeddings


def build_sparse(indexed, **kwargs):
    _, control, _, body, _, _ = indexed
    builder = kwargs.pop("service_override", None) or sparse_service(control, **kwargs)
    index_id = builder.schedule(UUID(body["job_id"]))
    assert index_id
    builder.run(index_id)
    return index_id, builder


def principal(control, credentials, role="curator") -> Principal:
    return control.auth.authenticate(auth(credentials)["Authorization"])


# ----------------------------------------------------------------- lexical index build


def test_pipeline_reaches_retrieval_ready_with_a_verified_lexical_index(indexed):
    client, control, credentials, body, embedding_run_id, _ = indexed
    index_id, _ = build_sparse(indexed)

    with control.sessions() as session:
        index = session.get(SparseIndex, index_id)
        findings = [
            (row.severity, row.code, row.message, row.details)
            for row in session.scalars(
                select(SparseValidationFinding).where(
                    SparseValidationFinding.sparse_index_id == index_id
                )
            )
        ]
        assert index.status == "VERIFIED" and index.is_active, (index.error_code, findings)
        assert (
            index.verified_chunk_count
            == index.indexed_chunk_count
            == index.expected_chunk_count
            > 0
        )
        assert index.term_count > 0 and index.posting_count > 0 and index.total_length > 0
        assert index.corpus_fingerprint and index.activated_at is not None
        # The lexical corpus is the dense lane's corpus, by construction and by verification.
        embedded = set(
            session.scalars(
                select(ChunkEmbedding.chunk_id).where(
                    ChunkEmbedding.embedding_run_id == embedding_run_id
                )
            )
        )
        lexical = set(
            session.scalars(
                select(SparseDocument.chunk_id).where(SparseDocument.sparse_index_id == index_id)
            )
        )
        assert embedded == lexical
        assert not session.scalars(
            select(SparseValidationFinding).where(
                SparseValidationFinding.sparse_index_id == index_id,
                SparseValidationFinding.severity.in_(("CRITICAL", "ERROR")),
            )
        ).all()

    job = client.get(f"/api/v1/ingestion/jobs/{body['job_id']}", headers=auth(credentials)).json()
    assert job["status"] == "RETRIEVAL_READY"
    assert [event["to_status"] for event in job["events"]][-3:] == [
        "SPARSE_INDEXING",
        "VERIFYING_SPARSE_INDEX",
        "RETRIEVAL_READY",
    ]


def test_parent_chunks_stay_out_of_the_lexical_lane_too(indexed):
    _, control, _, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    with control.sessions() as session:
        types = set(
            session.scalars(
                select(SparseDocument.chunk_type).where(SparseDocument.sparse_index_id == index_id)
            )
        )
    assert types and "TEXT_PARENT" not in types


def test_term_statistics_agree_with_the_stored_postings(indexed):
    _, control, _, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    with control.sessions() as session:
        recomputed = {
            term: (int(documents), int(total))
            for term, documents, total in session.execute(
                select(
                    SparsePosting.term,
                    func.count(),
                    func.sum(SparsePosting.term_frequency),
                )
                .where(SparsePosting.sparse_index_id == index_id)
                .group_by(SparsePosting.term)
            ).all()
        }
        stored = {
            term: (documents, total)
            for term, documents, total in session.execute(
                select(
                    SparseTerm.term, SparseTerm.document_frequency, SparseTerm.total_frequency
                ).where(SparseTerm.sparse_index_id == index_id)
            ).all()
        }
    assert recomputed and recomputed == stored


def test_the_analyzer_version_is_recorded_and_shared(indexed):
    _, control, _, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    with control.sessions() as session:
        index = session.get(SparseIndex, index_id)
        version = session.get(SparseIndexVersion, index.sparse_index_version_id)
        assert version.analyzer_fingerprint == ANALYZER.analyzer_fingerprint
        assert version.stopword_policy == "DISABLED"
        assert version.expansion == "NONE"


def test_a_second_build_supersedes_the_first_without_deleting_its_history(indexed):
    _, control, credentials, body, _, _ = indexed
    first, _ = build_sparse(indexed)
    actor = principal(control, credentials)
    builder = sparse_service(control)
    second = builder.request(actor, UUID(body["job_id"]), uuid4())
    assert second and second != first
    builder.run(second)
    with control.sessions() as session:
        assert session.get(SparseIndex, first).status == "SUPERSEDED"
        assert not session.get(SparseIndex, first).is_active
        assert session.get(SparseIndex, second).is_active
        assert session.get(IngestionJob, UUID(body["job_id"])).status == Status.RETRIEVAL_READY


def test_a_duplicate_schedule_returns_the_same_index(indexed):
    _, control, _, body, _, _ = indexed
    builder = sparse_service(control)
    first = builder.schedule(UUID(body["job_id"]))
    assert builder.schedule(UUID(body["job_id"])) == first


def test_a_stale_outbox_delivery_never_starts_a_second_build(indexed):
    _, control, _, body, _, _ = indexed
    builder = sparse_service(control)
    index_id = builder.schedule(UUID(body["job_id"]))
    with control.sessions() as session:
        message = session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.job_id == UUID(body["job_id"]), OutboxMessage.kind == "SPARSE_INDEX"
            )
        )
        message_id = message.id
    assert receive(control.sessions, control.storage, message_id) is not None
    # The same delivery again is a duplicate and must be discarded rather than replayed.
    assert receive(control.sessions, control.storage, message_id) is None
    builder.run(index_id)
    with control.sessions() as session:
        assert session.get(SparseIndex, index_id).status == "VERIFIED"


def test_an_expired_lease_fails_the_build_rather_than_leaving_it_stuck(indexed):
    _, control, _, body, _, _ = indexed
    builder = sparse_service(control)
    index_id = builder.schedule(UUID(body["job_id"]))
    claim = builder._claim(index_id)
    assert claim
    token, _ = claim
    with control.sessions.begin() as session:
        session.execute(
            update(SparseIndex)
            .where(SparseIndex.id == index_id)
            .values(lease_expires_at=func.now() - func.make_interval(0, 0, 0, 0, 1))
        )
    assert builder.sweep() >= 1
    with control.sessions() as session:
        index = session.get(SparseIndex, index_id)
        assert index.status == "FAILED" and index.error_code == "SPARSE_LEASE_EXPIRED"
        assert session.get(IngestionJob, UUID(body["job_id"])).status == Status.FAILED
    _ = token


def test_a_lexical_index_cannot_be_built_without_a_verified_dense_index(indexed):
    _, control, _, body, embedding_run_id, _ = indexed
    with control.sessions.begin() as session:
        session.execute(
            update(IndexRun)
            .where(IndexRun.embedding_run_id == embedding_run_id)
            .values(is_active=False)
        )
    builder = sparse_service(control)
    assert builder.schedule(UUID(body["job_id"])) is None
    with control.sessions() as session:
        job = session.get(IngestionJob, UUID(body["job_id"]))
        assert job.status == Status.FAILED
        assert job.last_error_code == "SPARSE_SOURCE_NOT_READY"


def test_completed_lexical_indexes_are_immutable_at_the_database(indexed):
    _, control, _, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    with pytest.raises(DBAPIError):
        with control.sessions.begin() as session:
            session.execute(
                update(SparseIndex).where(SparseIndex.id == index_id).values(term_count=0)
            )


def test_postings_cannot_be_added_to_a_completed_index(indexed):
    _, control, _, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    with pytest.raises(DBAPIError):
        with control.sessions.begin() as session:
            session.add(
                SparsePosting(
                    sparse_index_id=index_id, chunk_id=uuid4(), term="injected", term_frequency=1
                )
            )


def test_validation_findings_are_append_only(indexed):
    _, control, _, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    with control.sessions.begin() as session:
        session.add(
            SparseValidationFinding(
                sparse_index_id=index_id,
                severity="INFO",
                code="TEST",
                message="Recorded for the append-only assertion.",
                details={},
            )
        )
    with pytest.raises(DBAPIError):
        with control.sessions.begin() as session:
            session.execute(
                update(SparseValidationFinding)
                .where(SparseValidationFinding.code == "TEST")
                .values(message="rewritten")
            )


def test_superseding_the_chunk_dataset_deactivates_the_lexical_index(indexed):
    _, control, _, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    with control.sessions() as session:
        chunk_run_id = session.get(SparseIndex, index_id).chunk_run_id
    with control.sessions.begin() as session:
        session.execute(update(ChunkRun).where(ChunkRun.id == chunk_run_id).values(is_active=False))
    with control.sessions() as session:
        index = session.get(SparseIndex, index_id)
        assert not index.is_active and index.status == "SUPERSEDED"


# ------------------------------------------------------------------- corpus resolution


def test_the_corpus_lists_only_versions_whose_two_lanes_agree(indexed):
    _, control, credentials, _, embedding_run_id, _ = indexed
    index_id, _ = build_sparse(indexed)
    actor = principal(control, credentials)
    with control.sessions() as session:
        corpus = resolve_corpus(session, actor.tenant_id)
        assert corpus.document_version_ids
        assert corpus.embedding_run_ids == (embedding_run_id,)
        assert corpus.sparse_index_ids == (index_id,)
        assert len(set(corpus.chunk_run_ids)) == 1
        assert corpus.collection and corpus.vector_name == "medcpt_dense"


def test_a_version_without_a_lexical_index_is_not_in_the_corpus(indexed):
    _, control, credentials, _, _, _ = indexed
    actor = principal(control, credentials)
    with control.sessions() as session:
        # The dense lane is verified but the lexical build has not run yet.
        assert resolve_corpus(session, actor.tenant_id).empty


def test_a_lane_disagreement_about_the_chunk_dataset_fails_closed(indexed):
    _, control, credentials, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    actor = principal(control, credentials)
    with control.sessions.begin() as session:
        # Force the inconsistency the guards are supposed to make impossible, and prove the
        # retrieval path refuses it rather than fusing candidates from two corpus versions.
        session.execute(
            update(IndexRun).where(IndexRun.is_active.is_(True)).values(chunk_run_id=uuid4())
        )
    with control.sessions() as session, pytest.raises(RetrievalError) as raised:
        resolve_corpus(session, actor.tenant_id)
    assert raised.value.code == "RETRIEVAL_CORPUS_MISALIGNED"
    _ = index_id


def test_an_analyzer_change_is_refused_until_the_lexical_index_is_rebuilt(indexed, qdrant):
    _, control, credentials, _, _, _ = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    changed = SparseAnalyzerConfig(case_policy="NORMALIZED")
    retrieval = retrieval_service(
        control, StubQueryEncoder(QueryEncoderConfig()), qdrant, analyzer=changed
    )
    with pytest.raises(RetrievalError) as raised:
        retrieval.search(actor, "sepsis", uuid4(), mode="BM25_ONLY")
    assert raised.value.code == "SPARSE_INDEX_VERSION_MISMATCH"


# ------------------------------------------------------------------------ lexical search


def _first_chunk(control, index_id):
    with control.sessions() as session:
        chunk_id = session.scalar(
            select(SparseDocument.chunk_id)
            .where(SparseDocument.sparse_index_id == index_id)
            .order_by(SparseDocument.chunk_id)
        )
        return session.get(Chunk, chunk_id)


def test_lexical_search_finds_a_chunk_by_its_own_text(indexed):
    _, control, credentials, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    actor = principal(control, credentials)
    chunk = _first_chunk(control, index_id)
    with control.sessions() as session:
        corpus = resolve_corpus(session, actor.tenant_id)
        ranking = PostgresSparseRetriever(session, ANALYZER, RetrievalConfig()).search(
            chunk.retrieval_text[:200], corpus=corpus, top_k=10
        )
    assert ranking.hits
    assert chunk.id in {hit.chunk_id for hit in ranking.hits}
    assert ranking.hits[0].matched_terms


def test_lexical_search_returns_nothing_for_a_query_with_no_analyzable_terms(indexed):
    _, control, credentials, _, _, _ = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    with control.sessions() as session:
        corpus = resolve_corpus(session, actor.tenant_id)
        ranking = PostgresSparseRetriever(session, ANALYZER, RetrievalConfig()).search(
            "!!! ???", corpus=corpus, top_k=10
        )
    assert ranking.hits == ()


def test_lexical_search_is_confined_to_the_supplied_indexes(indexed):
    """A tenant's corpus is the only thing searchable; an empty corpus searches nothing."""
    _, control, credentials, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    actor = principal(control, credentials)
    chunk = _first_chunk(control, index_id)
    with control.sessions() as session:
        corpus = resolve_corpus(session, actor.tenant_id)
        foreign = type(corpus)(
            tenant_id=uuid4(),
            document_version_ids=corpus.document_version_ids,
            embedding_run_ids=corpus.embedding_run_ids,
            index_run_ids=corpus.index_run_ids,
            sparse_index_ids=corpus.sparse_index_ids,
            chunk_run_ids=corpus.chunk_run_ids,
            embedding_version_id=corpus.embedding_version_id,
            sparse_index_version_id=corpus.sparse_index_version_id,
            collection=corpus.collection,
            vector_name=corpus.vector_name,
        )
        ranking = PostgresSparseRetriever(session, ANALYZER, RetrievalConfig()).search(
            chunk.retrieval_text[:200], corpus=foreign, top_k=10
        )
    # Even with the right index ids, a different tenant sees nothing: the tenant condition is
    # asserted in the query rather than merely implied by the index scope.
    assert ranking.hits == ()


def test_a_superseded_lexical_index_is_never_searched(indexed):
    _, control, credentials, body, _, _ = indexed
    first, _ = build_sparse(indexed)
    actor = principal(control, credentials)
    builder = sparse_service(control)
    second = builder.request(actor, UUID(body["job_id"]), uuid4())
    builder.run(second)
    with control.sessions() as session:
        corpus = resolve_corpus(session, actor.tenant_id)
    assert first not in corpus.sparse_index_ids
    assert second in corpus.sparse_index_ids


def test_a_query_broader_than_the_scan_limit_is_refused_not_truncated(indexed):
    _, control, credentials, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    actor = principal(control, credentials)
    chunk = _first_chunk(control, index_id)
    config = RetrievalConfig().model_copy(update={"max_scanned_postings": 1})
    with control.sessions() as session:
        corpus = resolve_corpus(session, actor.tenant_id)
        with pytest.raises(RetrievalError) as raised:
            PostgresSparseRetriever(session, ANALYZER, config).search(
                chunk.retrieval_text, corpus=corpus, top_k=10
            )
    assert raised.value.code == "SPARSE_QUERY_TOO_BROAD"


# ------------------------------------------------------------------------ hybrid retrieval


def _target_vector(control, embedding_run_id, qdrant, embeddings):
    """The stored vector of one indexed chunk, so the dense answer is known in advance."""
    with control.sessions() as session:
        record = session.scalars(
            select(ChunkEmbedding)
            .where(ChunkEmbedding.embedding_run_id == embedding_run_id)
            .order_by(ChunkEmbedding.chunk_id)
        ).first()
        collection = session.scalar(
            select(IndexRun.physical_collection).where(
                IndexRun.embedding_run_id == embedding_run_id
            )
        )
    stored = qdrant.retrieve(embeddings.schema(collection), (record.point_id,))
    return record.chunk_id, stored[0].vector


def test_hybrid_search_returns_hydrated_candidates_with_full_provenance(indexed, qdrant):
    _, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    chunk_id, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    retrieval = retrieval_service(control, StubQueryEncoder(QueryEncoderConfig(), vector), qdrant)
    result = retrieval.search(actor, "a question about the corpus", uuid4())

    assert result.mode == "HYBRID_RRF"
    assert result.candidates

    # The query vector *is* this chunk's stored vector, so the dense lane must rank it first.
    # Deliberately not asserted of the fused rank: RRF rewards agreement between lanes, so a
    # chunk placed second by both lanes legitimately outranks one placed first by dense alone
    # (2/62 > 1/61). Asserting a fused rank here would be asserting the opposite of what fusion
    # is for, and would pass or fail depending on which chunk the fixture happened to pick.
    assert result.dense[0].chunk_id == chunk_id
    target = next(item for item in result.candidates if item.chunk_id == chunk_id)
    assert target.dense_rank == 1
    assert "DENSE" in target.lanes

    top = result.candidates[0]
    assert top.provenance.document_id and top.provenance.document_version_id
    assert top.provenance.page_start is not None
    assert top.provenance.source_type and top.provenance.authority_level
    assert top.preview
    assert result.trace.dense_candidates > 0
    assert result.trace.index_run_ids and result.trace.sparse_index_ids
    assert result.trace.query_encoder_version_id is not None
    assert result.trace.embedding_version_id is not None


def test_the_query_encoder_version_is_recorded_once_and_reused(indexed, qdrant):
    _, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    _, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    retrieval = retrieval_service(control, StubQueryEncoder(QueryEncoderConfig(), vector), qdrant)
    retrieval.search(actor, "first question", uuid4())
    retrieval.search(actor, "second question", uuid4())
    with control.sessions() as session:
        versions = session.scalars(select(QueryEncoderVersion)).all()
    assert len(versions) == 1
    assert versions[0].model_id == "ncbi/MedCPT-Query-Encoder"
    assert versions[0].max_query_tokens == 64 and versions[0].pooling_strategy == "CLS"


def test_an_encoder_that_is_not_the_pinned_revision_is_refused(indexed, qdrant):
    _, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    _, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    retrieval = retrieval_service(control, DivergentEncoder(QueryEncoderConfig(), vector), qdrant)
    with pytest.raises(RetrievalError) as raised:
        retrieval.search(actor, "a question", uuid4())
    assert raised.value.code == "QUERY_ENCODER_REVISION_MISMATCH"


def test_dense_only_and_bm25_only_modes_use_exactly_one_lane(indexed, qdrant):
    _, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    _, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    encoder = StubQueryEncoder(QueryEncoderConfig(), vector)
    retrieval = retrieval_service(control, encoder, qdrant)

    dense = retrieval.search(actor, "a question", uuid4(), mode="DENSE_ONLY")
    assert dense.dense and not dense.sparse
    assert all(hit.sparse_rank is None for hit in dense.candidates)

    before = encoder.calls
    lexical = retrieval.search(actor, "a question", uuid4(), mode="BM25_ONLY")
    assert lexical.sparse and not lexical.dense
    # A lexical-only query must not encode anything: no model call, no encoder dependency.
    assert encoder.calls == before
    assert lexical.trace.query_encoder_version_id is None


def test_hybrid_fails_closed_when_the_lexical_lane_is_unavailable(indexed, qdrant):
    _, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    _, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    retrieval = retrieval_service(
        control,
        StubQueryEncoder(QueryEncoderConfig(), vector),
        qdrant,
        config=RetrievalConfig().model_copy(update={"max_scanned_postings": 1}),
    )
    with control.sessions() as session:
        chunk_id = session.scalar(select(SparseDocument.chunk_id).limit(1))
        text = session.get(Chunk, chunk_id).retrieval_text
    with pytest.raises(RetrievalError) as raised:
        retrieval.search(actor, text, uuid4())
    assert raised.value.code == "SPARSE_QUERY_TOO_BROAD"


def test_degradation_to_dense_only_is_explicit_and_reported(indexed, qdrant):
    _, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    _, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    retrieval = retrieval_service(
        control,
        StubQueryEncoder(QueryEncoderConfig(), vector),
        qdrant,
        config=RetrievalConfig(degradation_policy="ALLOW_DENSE_ONLY").model_copy(
            update={"max_scanned_postings": 1}
        ),
    )
    with control.sessions() as session:
        chunk_id = session.scalar(select(SparseDocument.chunk_id).limit(1))
        text = session.get(Chunk, chunk_id).retrieval_text
    result = retrieval.search(actor, text, uuid4())
    assert "SPARSE_LANE_UNAVAILABLE_DEGRADED_TO_DENSE" in result.warnings
    assert result.candidates


def test_retrieval_is_refused_when_no_version_is_retrieval_ready(indexed, qdrant):
    _, control, credentials, _, _, _ = indexed
    actor = principal(control, credentials)
    retrieval = retrieval_service(control, StubQueryEncoder(QueryEncoderConfig()), qdrant)
    with pytest.raises(RetrievalError) as raised:
        retrieval.search(actor, "a question", uuid4())
    assert raised.value.code == "RETRIEVAL_CORPUS_EMPTY"


def test_filters_narrow_the_candidate_set_server_side(indexed, qdrant):
    _, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    _, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    retrieval = retrieval_service(control, StubQueryEncoder(QueryEncoderConfig(), vector), qdrant)
    unfiltered = retrieval.search(actor, "a question", uuid4())
    filtered = retrieval.search(
        actor,
        "a question",
        uuid4(),
        filters=RetrievalFilters(source_types=("QUESTION_PAPER",)),
    )
    assert unfiltered.candidates
    assert len(filtered.candidates) < len(unfiltered.candidates)


def test_hydration_refuses_a_chunk_outside_the_resolved_corpus(indexed):
    _, control, credentials, _, _, _ = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    with control.sessions() as session:
        corpus = resolve_corpus(session, actor.tenant_id)
        assert hydrate(session, actor.tenant_id, (uuid4(),), corpus, 200) == {}
        assert hydrate(session, uuid4(), corpus.chunk_run_ids, corpus, 200) == {}


# --------------------------------------------------------------------------- API surface


def test_search_requires_the_retrieval_permission(indexed, qdrant):
    client, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    _, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    control.retrieval._encoder = StubQueryEncoder(QueryEncoderConfig(), vector)
    control.retrieval._encoder_factory = lambda: control.retrieval._encoder
    control.retrieval._index_factory = lambda: qdrant

    reader = next(item for item in credentials if item.role == "reader")
    response = client.post(
        "/api/v1/retrieval/search",
        json={"query": "a question"},
        headers=auth([reader]),
    )
    assert response.status_code == 403


def test_search_response_carries_candidates_and_no_answer(indexed, qdrant):
    client, control, credentials, _, embedding_run_id, embeddings = indexed
    build_sparse(indexed)
    _, vector = _target_vector(control, embedding_run_id, qdrant, embeddings)
    control.retrieval._encoder = StubQueryEncoder(QueryEncoderConfig(), vector)
    control.retrieval._encoder_factory = lambda: control.retrieval._encoder
    control.retrieval._index_factory = lambda: qdrant

    response = client.post(
        "/api/v1/retrieval/search",
        json={"query": "a question about the corpus", "mode": "HYBRID_RRF", "top_k": 5},
        headers=auth(credentials),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answering_enabled"] is False
    assert "answer" not in body and "confidence" not in body
    assert body["candidates"]
    assert body["trace"]["query_hash"] and "query" not in body["trace"]
    for candidate in body["candidates"]:
        assert candidate["provenance"]["document_id"]
        assert "vector" not in candidate and "embedding" not in candidate


def test_the_search_body_cannot_carry_a_tenant(indexed, qdrant):
    client, _, credentials, _, _, _ = indexed
    response = client.post(
        "/api/v1/retrieval/search",
        json={"query": "a question", "tenant_id": str(uuid4())},
        headers=auth(credentials),
    )
    assert response.status_code == 422


def test_status_reports_the_corpus_without_running_a_query(indexed, qdrant):
    client, control, credentials, _, _, _ = indexed
    build_sparse(indexed)
    response = client.get("/api/v1/retrieval/status", headers=auth(credentials))
    assert response.status_code == 200
    body = response.json()
    assert body["answering_enabled"] is False
    assert body["document_versions"] == 1 and body["sparse_indexes"] == 1
    assert body["default_mode"] == "HYBRID_RRF"
    assert set(body["modes"]) == {"DENSE_ONLY", "BM25_ONLY", "HYBRID_RRF"}
    assert body["degradation_policy"] == "FAIL_CLOSED"


def test_the_sparse_index_summary_reports_lane_alignment(indexed):
    client, control, credentials, body, _, _ = indexed
    build_sparse(indexed)
    document = client.get("/api/v1/documents?limit=1", headers=auth(credentials)).json()["items"][0]
    version_id = document["latest_version"]["id"]
    summary = client.get(
        f"/api/v1/documents/{document['id']}/versions/{version_id}/sparse-index",
        headers=auth(credentials),
    ).json()
    assert summary["retrieval_ready"] is True and summary["lanes_aligned"] is True
    assert summary["sparse_index"]["status"] == "VERIFIED"
    assert summary["sparse_index_version"]["expansion"] == "NONE"
    assert summary["chunk_types"]
    _ = body


def test_a_lexical_rebuild_requires_its_own_permission(indexed):
    client, _, credentials, body, _, _ = indexed
    build_sparse(indexed)
    reader = next(item for item in credentials if item.role == "reader")
    response = client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/reindex-sparse",
        json={},
        headers=auth([reader]),
    )
    assert response.status_code == 403


# ------------------------------------------------------------------ the real encoders


@pytest.mark.skipif(not CACHE.exists(), reason="Run scripts/provision_embedding_model.py first")
def test_real_encoders_retrieve_a_passage_from_its_own_wording(chunked, qdrant):
    """The whole path with both pinned models: parse, chunk, embed, index, analyze, then search."""
    from app.embeddings.medcpt import MedCPTArticleEmbedder
    from app.retrieval.query.medcpt import MedCPTQueryEncoder
    from app.services.embedding import EmbeddingService

    _, control, credentials, body = chunked
    config = embedding_config(model_cache_dir=CACHE, offline=True, batch_size=4)
    embeddings = EmbeddingService(
        control.sessions,
        config,
        index_config(),
        model_factory=lambda: MedCPTArticleEmbedder(config),
        index_factory=lambda: qdrant,
    )
    run_id = embeddings.schedule(UUID(body["job_id"]))
    embeddings.run(run_id)
    builder = sparse_service(control)
    index_id = builder.schedule(UUID(body["job_id"]))
    builder.run(index_id)
    with control.sessions() as session:
        assert session.get(SparseIndex, index_id).status == "VERIFIED"
        assert session.get(IngestionJob, UUID(body["job_id"])).status == Status.RETRIEVAL_READY
        chunk_id = session.scalar(
            select(SparseDocument.chunk_id)
            .where(SparseDocument.sparse_index_id == index_id)
            .order_by(SparseDocument.chunk_id)
        )
        text = session.get(Chunk, chunk_id).retrieval_text

    encoder = MedCPTQueryEncoder(QueryEncoderConfig(model_cache_dir=CACHE, offline=True))
    retrieval = retrieval_service(control, encoder, qdrant)
    actor = principal(control, credentials)
    # A short question built from the passage's own wording, inside the 64-token query limit.
    query = " ".join(text.split()[:20])
    result = retrieval.search(actor, query, uuid4(), mode="HYBRID_RRF")
    assert result.candidates
    assert chunk_id in {candidate.chunk_id for candidate in result.candidates}
    assert result.trace.query_token_count and result.trace.query_token_count <= 64
    assert result.trace.durations_ms["encoding_ms"] > 0


@pytest.mark.skipif(not CACHE.exists(), reason="Run scripts/provision_embedding_model.py first")
def test_a_question_longer_than_the_encoder_accepts_is_rejected(indexed, qdrant):
    from app.retrieval.query.medcpt import MedCPTQueryEncoder

    _, control, credentials, _, _, _ = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    encoder = MedCPTQueryEncoder(QueryEncoderConfig(model_cache_dir=CACHE, offline=True))
    retrieval = retrieval_service(control, encoder, qdrant)
    long_query = "chronic kidney disease and heart failure management considerations " * 12
    with pytest.raises(RetrievalError) as raised:
        retrieval.search(actor, long_query, uuid4())
    assert raised.value.code == "QUERY_TOO_LONG"
    assert raised.value.details["limit"] == 64


def test_a_domain_error_is_raised_rather_than_an_unknown_mode_being_guessed(indexed, qdrant):
    _, control, credentials, _, _, _ = indexed
    build_sparse(indexed)
    actor = principal(control, credentials)
    retrieval = retrieval_service(control, StubQueryEncoder(QueryEncoderConfig()), qdrant)
    with pytest.raises(DomainError):
        retrieval.search(actor, "a question", uuid4(), mode="LEARNED_FUSION")


def test_activation_time_is_written_once_and_survives_superseding(indexed):
    from datetime import UTC, datetime, timedelta

    _, control, credentials, body, _, _ = indexed
    first, builder = build_sparse(indexed)
    with control.sessions() as session:
        activated = session.get(SparseIndex, first).activated_at
        assert activated is not None
    for changed in (None, datetime.now(UTC) + timedelta(days=1)):
        with pytest.raises(DBAPIError), control.sessions.begin() as session:
            session.execute(
                update(SparseIndex).where(SparseIndex.id == first).values(activated_at=changed)
            )
    second = builder.request(principal(control, credentials), UUID(body["job_id"]), uuid4())
    builder.run(second)
    with control.sessions() as session:
        assert session.get(SparseIndex, first).activated_at == activated
        assert session.get(SparseIndex, first).status == "SUPERSEDED"
        assert session.get(SparseIndex, second).is_active


def test_failed_replacement_preserves_previous_verified_index(indexed, monkeypatch):
    from app.retrieval.validation import Finding, SparseReconciliation

    _, control, credentials, body, _, _ = indexed
    first, builder = build_sparse(indexed)
    second = builder.request(principal(control, credentials), UUID(body["job_id"]), uuid4())
    monkeypatch.setattr(
        "app.services.sparse_index.reconcile_sparse",
        lambda *args: SparseReconciliation(
            findings=(
                Finding("CRITICAL", "SPARSE_TEST_FAILURE", "Synthetic reconciliation failure."),
            ),
            verified=0,
            expected=1,
            corpus_fingerprint="failed",
        ),
    )
    builder.run(second)
    with control.sessions() as session:
        assert session.get(SparseIndex, first).is_active
        assert session.get(SparseIndex, first).status == "VERIFIED"
        assert session.get(SparseIndex, second).status == "FAILED"
        assert not session.get(SparseIndex, second).is_active


def test_concurrent_query_encoder_registration_is_idempotent(indexed, qdrant):
    from concurrent.futures import ThreadPoolExecutor

    _, control, _, _, _, _ = indexed
    retrieval = retrieval_service(control, StubQueryEncoder(QueryEncoderConfig()), qdrant)
    spec = StubQueryEncoder(QueryEncoderConfig()).specification

    def register(_):
        with control.sessions.begin() as session:
            return retrieval.encoder_version(session, spec).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(register, range(2)))
    assert ids[0] == ids[1]


def test_hydration_rejects_another_chunk_run_even_on_the_same_version(indexed):
    from dataclasses import replace

    _, control, credentials, _, _, _ = indexed
    index_id, _ = build_sparse(indexed)
    chunk = _first_chunk(control, index_id)
    with control.sessions() as session:
        corpus = resolve_corpus(session, principal(control, credentials).tenant_id)
        stale = replace(corpus, chunk_run_ids=(uuid4(),))
        assert not hydrate(session, corpus.tenant_id, (chunk.id,), stale, 200)


def test_receipt_before_claim_crash_is_recovered(indexed):
    from datetime import UTC, datetime, timedelta

    _, control, _, body, _, _ = indexed
    builder = sparse_service(control)
    index_id = builder.schedule(UUID(body["job_id"]))
    with control.sessions() as session:
        message_id = session.scalar(
            select(OutboxMessage.id).where(OutboxMessage.sparse_index_id == index_id)
        )
    assert receive(control.sessions, control.storage, message_id) == UUID(body["job_id"])
    with control.sessions.begin() as session:
        session.get(OutboxMessage, message_id).received_at = datetime.now(UTC) - timedelta(
            minutes=2
        )
    builder.sweep()
    assert receive(control.sessions, control.storage, message_id) == UUID(body["job_id"])
    builder.run(index_id)
    with control.sessions() as session:
        assert session.get(SparseIndex, index_id).is_active
