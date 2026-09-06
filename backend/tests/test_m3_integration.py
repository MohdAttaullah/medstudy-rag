"""Real PostgreSQL/MinIO/API tests for atomic M3 datasets and authorization."""

import os
from uuid import UUID, uuid4

import pytest
from app.models.chunking import (
    Chunk,
    ChunkRelation,
    ChunkRun,
    ChunkSourceElement,
    QuestionArtifact,
    QuestionOption,
)
from app.models.documents import OutboxMessage
from app.models.enums import Status
from app.models.parsing import DocumentElement
from app.services import chunking as chunking_service
from app.services.queue import receive
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from tests.test_m1_integration import auth, database  # noqa: F401
from tests.test_m2_integration import (
    Recorded,
    artifact_stub,
    make_service,
    queue_job,
)
from tests.test_m2_integration import (
    system_module as system_module,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires PostgreSQL and MinIO"
    ),
]


@pytest.fixture
def prepared(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    outcome = make_service(control, parser_impl=Recorded(artifact_stub())).run(UUID(body["job_id"]))
    assert outcome.status == Status.READY_FOR_CHUNKING
    yield client, control, credentials, body
    client.post(f"/api/v1/ingestion/jobs/{body['job_id']}/cancel", headers=auth(credentials))


def execute(prepared):
    client, control, credentials, body = prepared
    run_id = control.chunks.schedule(UUID(body["job_id"]))
    assert run_id
    control.chunks.run(run_id)
    view = client.get(f"/api/v1/chunk-runs/{run_id}", headers=auth(credentials))
    assert view.status_code == 200, view.text
    assert view.json()["status"] == "SUCCEEDED", view.text
    return run_id


def test_atomic_dataset_provenance_api_and_m3_stop(prepared):
    client, control, credentials, body = prepared
    run_id = execute(prepared)
    rows = client.get(
        f"/api/v1/chunk-runs/{run_id}/chunks?limit=100", headers=auth(credentials)
    ).json()["items"]
    assert {r["chunk_type"] for r in rows} >= {"TABLE", "FIGURE_CONTEXT", "FORMULA", "TEXT_CHILD"}
    for row in rows:
        sources = client.get(f"/api/v1/chunks/{row['id']}/sources", headers=auth(credentials))
        assert sources.status_code == 200, sources.text
        assert sources.json()["total"] > 0
        detail = client.get(f"/api/v1/chunks/{row['id']}", headers=auth(credentials)).json()
        assert "object_storage_key" not in str(detail)
    job = client.get(f"/api/v1/ingestion/jobs/{body['job_id']}", headers=auth(credentials)).json()
    assert job["status"] == "READY_FOR_EMBEDDING"
    assert [e["to_status"] for e in job["events"]][-3:] == [
        "CHUNKING",
        "VALIDATING_CHUNKS",
        "READY_FOR_EMBEDDING",
    ]


def test_duplicate_worker_and_unchanged_rechunk_reuse(prepared):
    client, control, credentials, body = prepared
    run_id = execute(prepared)
    control.chunks.run(run_id)
    response = client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/rechunk", json={}, headers=auth(credentials)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "READY_FOR_EMBEDDING"
    with control.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ChunkRun)
                .where(ChunkRun.ingestion_job_id == UUID(body["job_id"]))
            )
            == 1
        )


def test_forced_rechunk_identical_hashes_and_single_active(prepared):
    client, control, credentials, body = prepared
    first = execute(prepared)
    response = client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/rechunk",
        json={"force": True},
        headers=auth(credentials),
    )
    assert response.status_code == 200, response.text
    with control.sessions() as session:
        second = session.scalar(
            select(ChunkRun.id).where(
                ChunkRun.ingestion_job_id == UUID(body["job_id"]), ChunkRun.status == "PENDING"
            )
        )
    control.chunks.run(second)
    with control.sessions() as session:
        assert session.get(ChunkRun, second).status == "SUCCEEDED"
        assert not session.get(ChunkRun, first).is_active

        def hashes(run):
            return list(
                session.scalars(
                    select(Chunk.chunk_hash)
                    .where(Chunk.chunk_run_id == run)
                    .order_by(Chunk.sequence_number)
                )
            )

        assert hashes(first) == hashes(second)


@pytest.mark.parametrize("suffix", ["", "/chunks", "/questions", "/validation-findings"])
def test_tenant_scope_and_auth(prepared, suffix):
    client, control, credentials, body = prepared
    run_id = execute(prepared)
    path = f"/api/v1/chunk-runs/{run_id}{suffix}"
    assert client.get(path, headers=auth(credentials, 2)).status_code == 404
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth(credentials, 1)).status_code == 200
    assert (
        client.post(
            f"/api/v1/ingestion/jobs/{body['job_id']}/rechunk",
            json={},
            headers=auth(credentials, 1),
        ).status_code
        == 403
    )


def test_completed_chunks_are_immutable(prepared):
    _, control, _, _ = prepared
    run_id = execute(prepared)
    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        session.execute(
            update(Chunk).where(Chunk.chunk_run_id == run_id).values(normalized_text="tampered")
        )
    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        session.execute(
            update(ChunkSourceElement)
            .where(ChunkSourceElement.chunk_run_id == run_id)
            .values(end_offset=999999)
        )


def test_cancelled_delivery_never_builds(prepared):
    client, control, credentials, body = prepared
    run_id = control.chunks.schedule(UUID(body["job_id"]))
    client.post(f"/api/v1/ingestion/jobs/{body['job_id']}/cancel", headers=auth(credentials))
    control.chunks.run(run_id)
    with control.sessions() as session:
        assert session.get(ChunkRun, run_id).status == "CANCELLED"
        assert not session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.chunk_run_id == run_id)
        )


def test_chunk_outbox_receipt_and_duplicate(prepared):
    _, control, _, body = prepared
    run_id = control.chunks.schedule(UUID(body["job_id"]))
    with control.sessions() as session:
        message = session.scalar(select(OutboxMessage).where(OutboxMessage.chunk_run_id == run_id))
    assert receive(control.sessions, control.storage, message.id) == UUID(body["job_id"])
    assert receive(control.sessions, control.storage, message.id) is None
    control.chunks.run(run_id)
    with control.sessions() as session:
        assert session.get(ChunkRun, run_id).is_active


def test_reuse_requires_a_matching_input_fingerprint(prepared, monkeypatch):
    """A completed dataset is reused only while the normalized input still hashes the same."""
    client, control, credentials, body = prepared
    first = execute(prepared)
    with control.sessions() as session:
        run = session.get(ChunkRun, first)
        assert run.input_fingerprint
        assert control.chunks._reusable(session, run, control.chunks.config)

    # The stored input fingerprint no longer describes the current source.
    monkeypatch.setattr(chunking_service, "input_fingerprint", lambda source: "0" * 64)
    response = client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/rechunk", json={}, headers=auth(credentials)
    )
    assert response.status_code == 200, response.text
    with control.sessions() as session:
        assert not control.chunks._reusable(
            session, session.get(ChunkRun, first), control.chunks.config
        )
        pending = session.scalars(
            select(ChunkRun.id).where(
                ChunkRun.ingestion_job_id == UUID(body["job_id"]), ChunkRun.status == "PENDING"
            )
        ).all()
    assert len(pending) == 1 and pending[0] != first


def test_persistence_failure_leaves_no_partial_dataset(prepared, monkeypatch):
    client, control, credentials, body = prepared
    run_id = control.chunks.schedule(UUID(body["job_id"]))

    def explode(*args, **kwargs):
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(chunking_service.ChunkService, "_persist", explode)
    control.chunks.run(run_id)
    with control.sessions() as session:
        run = session.get(ChunkRun, run_id)
        assert run.status == "FAILED"
        assert run.error_code == "CHUNK_PERSISTENCE_FAILED"
        assert not run.is_active
        assert not session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.chunk_run_id == run_id)
        )
    job = client.get(f"/api/v1/ingestion/jobs/{body['job_id']}", headers=auth(credentials)).json()
    assert job["status"] == "FAILED"
    assert job["last_error_code"] == "CHUNK_PERSISTENCE_FAILED"


def test_completed_datasets_are_closed_to_further_rows(prepared):
    """A finished run is a historical record: nothing may be added to it afterwards."""
    _, control, _, _ = prepared
    run_id = execute(prepared)
    with control.sessions() as session:
        first, second = list(
            session.scalars(
                select(Chunk.id).where(Chunk.chunk_run_id == run_id).order_by(Chunk.sequence_number)
            )
        )[:2]
        parse_run_id = session.get(ChunkRun, run_id).parse_run_id
    with pytest.raises(DBAPIError, match="Completed chunk datasets cannot be changed"):
        with control.sessions.begin() as session:
            session.add(
                ChunkRelation(
                    chunk_run_id=run_id,
                    source_id=first,
                    target_id=second,
                    relation="NEXT_SIBLING",
                )
            )
            session.flush()
    with pytest.raises(DBAPIError, match="Completed chunk datasets cannot be changed"):
        with control.sessions.begin() as session:
            session.execute(
                update(ChunkSourceElement)
                .where(ChunkSourceElement.chunk_id == first)
                .values(role="TAMPERED")
            )
    assert parse_run_id


def test_database_rejects_structurally_invalid_rows(prepared):
    """The structural invariants hold in the schema, not only in the service."""
    _, control, _, body = prepared
    run_id = control.chunks.schedule(UUID(body["job_id"]))
    claim = control.chunks._claim(run_id)
    assert claim
    with control.sessions() as session:
        run = session.get(ChunkRun, run_id)
        parse_run_id, tenant_id = run.parse_run_id, run.tenant_id
        element_id, element_length = session.execute(
            select(DocumentElement.id, func.length(DocumentElement.normalized_text))
            .where(
                DocumentElement.parse_run_id == parse_run_id,
                DocumentElement.normalized_text.is_not(None),
            )
            .limit(1)
        ).one()

    def chunk(**overrides):
        values = dict(
            tenant_id=tenant_id,
            chunk_run_id=run_id,
            parse_run_id=parse_run_id,
            chunk_type="TEXT_CHILD",
            sequence_number=9001,
            raw_text="x",
            normalized_text="x",
            retrieval_text="x",
            token_count=1,
            retrieval_token_count=1,
            page_start=1,
            page_end=1,
            chunk_hash="f" * 64,
            chunk_metadata={},
        )
        return Chunk(**{**values, **overrides})

    def rejected(build, match):
        with pytest.raises(DBAPIError, match=match), control.sessions.begin() as session:
            session.add(build())
            session.flush()

    rejected(lambda: chunk(page_start=4, page_end=1), "chunk_page_range")
    rejected(lambda: chunk(token_count=-1), "chunk_tokens_nonnegative")
    rejected(lambda: chunk(parent_chunk_id=uuid4()), "chunks")  # foreign key to the same run

    with control.sessions.begin() as session:
        session.add(chunk())
        session.flush()
        valid = session.scalar(
            select(Chunk.id).where(Chunk.chunk_run_id == run_id, Chunk.sequence_number == 9001)
        )
    rejected(
        lambda: ChunkSourceElement(
            chunk_id=valid,
            chunk_run_id=run_id,
            parse_run_id=parse_run_id,
            element_id=element_id,
            position=0,
            start_offset=0,
            end_offset=element_length + 1000,
            role="SOURCE",
        ),
        "Source offsets do not resolve",
    )
    rejected(
        lambda: ChunkRelation(
            chunk_run_id=run_id, source_id=valid, target_id=valid, relation="NEXT_SIBLING"
        ),
        "chunk_relation_not_self",
    )
    control.chunks._fail(run_id, claim[0], "CHUNK_VALIDATION_FAILED")


def test_question_options_keep_a_unique_order(prepared):
    _, control, _, _ = prepared
    run_id = execute(prepared)
    with control.sessions() as session:
        assert session.get(ChunkRun, run_id).status == "SUCCEEDED"
        for question in session.scalars(
            select(QuestionArtifact).where(QuestionArtifact.chunk_run_id == run_id)
        ):
            options = list(
                session.scalars(
                    select(QuestionOption)
                    .where(QuestionOption.question_id == question.id)
                    .order_by(QuestionOption.ordinal)
                )
            )
            assert [o.ordinal for o in options] == list(range(len(options)))
            assert len({o.label for o in options}) == len(options)
            assert not question.answer_inferred
