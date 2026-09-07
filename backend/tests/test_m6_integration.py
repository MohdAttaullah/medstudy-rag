"""M6 uses the actual PostgreSQL/Qdrant pipeline and authorized API boundary."""

import os
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from app.core.reranking_config import MODEL_FILES, RerankerConfig
from app.models.chunking import Chunk
from app.models.documents import Document
from app.repositories.evidence import EvidenceRepository
from app.repositories.retrieval import resolve_corpus
from app.reranking.medcpt import MedCPTReranker
from app.reranking.model import RerankerSpec, RerankingError, RerankResult, input_hash
from app.services.evidence import EvidenceService
from sqlalchemy import select, update
from tests.test_m1_integration import auth, database  # noqa: F401, F811
from tests.test_m2_integration import system_module as system_module
from tests.test_m4_integration import chunked, qdrant  # noqa: F401, F811
from tests.test_m5_integration import (  # noqa: F401, F811
    StubQueryEncoder,
    build_sparse,
    indexed,  # noqa: F811
    principal,
    retrieval_service,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]


class StubReranker:
    def __init__(self, callback=None):
        self.callback = callback
        self.inputs = []

    @property
    def specification(self):
        c = RerankerConfig()
        return RerankerSpec(
            model_id=c.model_id,
            model_revision=c.model_revision,
            tokenizer_revision=c.tokenizer_revision,
            files=MODEL_FILES,
            dtype=c.dtype,
            device=c.device,
            max_sequence_tokens=512,
            representation=c.representation,
            score_semantics=c.score_semantics,
            library_versions={"test": "stub"},
            config_fingerprint=c.fingerprint,
        )

    def rerank(self, query, candidates):
        self.inputs = list(candidates)
        if self.callback:
            self.callback()
        return [
            RerankResult(
                chunk_id=i.chunk_id,
                reranker_score=float(-i.fused_rank),
                input_hash=input_hash(query, i.text),
                token_count=50,
            )
            for i in candidates
        ]


@pytest.fixture
def pipeline(indexed, qdrant):  # noqa: F811 - pytest fixture imports
    build_sparse(indexed)
    client, control, credentials, body, _, _ = indexed
    retrieval = retrieval_service(control, StubQueryEncoder(control.settings.query_encoder), qdrant)
    reranker = StubReranker()
    control.evidence = EvidenceService(retrieval, control.settings, reranker)
    return client, control, credentials, body, reranker


def test_authorized_api_complete_provenance_and_full_inputs(pipeline):
    client, control, credentials, body, reranker = pipeline
    result = client.post(
        "/api/v1/retrieval/rerank",
        headers=auth(credentials),
        json={"query": "synthetic table formula"},
    )
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["answering_enabled"] is False
    assert data["first_stage"]["dense"] and data["first_stage"]["sparse"]
    assert data["reranked"] and data["evidence_set"]["evidence_blocks"]
    assert "answer" not in data and "confidence" not in data
    with control.sessions() as session:
        for item in reranker.inputs:
            assert item.text == session.get(Chunk, item.chunk_id).retrieval_text
        for b in data["evidence_set"]["evidence_blocks"]:
            assert b["document_version_id"] == body["version_id"]
            assert b["source_element_ids"] and b["source_spans"] and b["pages"]
            for artifact in b["artifacts"]:
                if artifact["kind"] == "TABLE":
                    assert artifact["cells"] and artifact["header_rows"]


def test_unauthorized_api_does_not_infer(pipeline):
    client, _, _, _, reranker = pipeline
    assert client.post("/api/v1/retrieval/rerank", json={"query": "test"}).status_code == 401
    assert not reranker.inputs


def test_corpus_change_during_inference_fails_closed(pipeline):
    _, control, credentials, body, reranker = pipeline
    from datetime import UTC, datetime

    def archive():
        with control.sessions.begin() as session:
            session.execute(
                update(Document)
                .where(Document.id == UUID(body["document_id"]))
                .values(archived_at=datetime.now(UTC))
            )

    reranker.callback = archive
    with pytest.raises(RerankingError, match="LINEAGE_MISMATCH"):
        control.evidence.search(principal(control, credentials), "table", uuid4())


def test_tenant_and_run_scope_for_expansion(pipeline):
    _, control, credentials, _, _ = pipeline
    actor = principal(control, credentials)
    with control.sessions() as session:
        corpus = resolve_corpus(session, actor.tenant_id)
        identifier = session.scalar(
            select(Chunk.id).where(Chunk.chunk_run_id.in_(corpus.chunk_run_ids))
        )
        with pytest.raises(RerankingError, match="LINEAGE_MISMATCH"):
            EvidenceRepository(session, uuid4(), corpus).source(identifier)
        with pytest.raises(RerankingError, match="LINEAGE_MISMATCH"):
            EvidenceRepository(
                session, actor.tenant_id, replace(corpus, chunk_run_ids=(uuid4(),))
            ).source(identifier)


def test_real_crossencoder_over_persisted_hybrid_candidates(pipeline):
    _, control, credentials, _, _ = pipeline
    control.evidence._reranker = MedCPTReranker(RerankerConfig())
    result = control.evidence.search(principal(control, credentials), "table formula", uuid4())
    assert result["reranked"] and result["evidence_set"]["total_tokens"] > 0
    assert result["evidence_set"]["reranking_trace"]["reranker"]["score_semantics"] == "RAW_LOGIT"


def test_reader_and_client_scope_injection_rejected(pipeline):
    client, _, credentials, _, reranker = pipeline
    assert (
        client.post(
            "/api/v1/retrieval/rerank", headers=auth(credentials, 1), json={"query": "test"}
        ).status_code
        == 403
    )
    for field, value in (
        ("tenant_id", str(uuid4())),
        ("candidate_ids", [str(uuid4())]),
        ("top_k", 40),
    ):
        assert (
            client.post(
                "/api/v1/retrieval/rerank",
                headers=auth(credentials),
                json={"query": "test", field: value},
            ).status_code
            == 422
        )
    assert not reranker.inputs
