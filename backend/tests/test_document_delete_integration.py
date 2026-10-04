"""Permanent deletion over the real pipeline, database, object store and vector index (ADR-026).

The document under test is carried through the real M2–M5 pipeline to RETRIEVAL_READY and cited by
a verified answer, so it has rows in every derived table, objects under its storage prefix and
points in Qdrant. Deletion is then judged by what is *absent* afterwards — in every store — and by
what was deliberately kept: the conversation, with its citations marked as deleted sources, and the
audit trail.
"""

import os
from uuid import UUID, uuid4

import pytest
from app.core.errors import DomainError
from app.models.chunking import (
    Chunk,
    ChunkRun,
    ChunkSourceElement,
    ChunkSourcePage,
    ChunkValidationFinding,
)
from app.models.documents import (
    AuditEvent,
    Document,
    DocumentDeletion,
    DocumentVersion,
    IngestionJob,
    IngestionStageEvent,
    OutboxMessage,
    UploadIntent,
)
from app.models.embeddings import ChunkEmbedding, EmbeddingRun, IndexRun
from app.models.parsing import DocumentElement, DocumentPage, ParseRun, ParseValidationFinding
from app.models.retrieval import SparseDocument, SparseIndex, SparsePosting, SparseTerm
from app.retrieval.errors import RetrievalError
from app.security.auth import Principal
from app.services.deletion import DELETED_TITLE
from app.services.storage import document_prefix
from sqlalchemy import func, select, text
from tests.test_m1_integration import auth, database  # noqa: F401 - fixtures
from tests.test_m2_integration import queue_job
from tests.test_m2_integration import system_module as system_module
from tests.test_m4_integration import chunked, qdrant  # noqa: F401, F811
from tests.test_m5_integration import indexed, principal  # noqa: F401, F811
from tests.test_m9_integration import ask, stack  # noqa: F401 - fixture import

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

CONFIRM = {"confirm": "DELETE"}
ADMIN, READER, OTHER_TENANT = 0, 1, 2


def delete(client, credentials, document_id, role=ADMIN, body=CONFIRM):
    return client.request(
        "DELETE", f"/api/v1/documents/{document_id}", headers=auth(credentials, role), json=body
    )


def objects(control, document_id):
    """Every object version and delete marker under the document's prefix."""
    found = []
    for page in control.storage.client.get_paginator("list_object_versions").paginate(
        Bucket=control.storage.bucket, Prefix=document_prefix(document_id)
    ):
        found += page.get("Versions", []) + page.get("DeleteMarkers", [])
    return found


def vectors(control, vector_index, collections, tenant_id, document_id):
    selector = {"tenant_id": str(tenant_id), "document_id": str(document_id)}
    return sum(
        vector_index.count(control.embeddings.schema(collection), selector)
        for collection in collections
    )


def owned_rows(control, document_id) -> dict[str, int]:
    """Row counts for every table that holds something of this document."""
    with control.sessions() as session:
        versions = select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
        jobs = select(IngestionJob.id).where(IngestionJob.document_version_id.in_(versions))
        parses = select(ParseRun.id).where(ParseRun.document_version_id.in_(versions))
        chunk_runs = select(ChunkRun.id).where(ChunkRun.document_id == document_id)
        embedding_runs = select(EmbeddingRun.id).where(EmbeddingRun.document_id == document_id)
        sparse = select(SparseIndex.id).where(SparseIndex.document_id == document_id)

        def count(model, clause):
            return int(session.scalar(select(func.count()).select_from(model).where(clause)) or 0)

        return {
            "documents": count(Document, Document.id == document_id),
            "versions": count(DocumentVersion, DocumentVersion.document_id == document_id),
            "jobs": count(IngestionJob, IngestionJob.id.in_(jobs)),
            "job_events": count(
                IngestionStageEvent, IngestionStageEvent.ingestion_job_id.in_(jobs)
            ),
            "outbox": count(OutboxMessage, OutboxMessage.job_id.in_(jobs)),
            "upload_intents": count(UploadIntent, UploadIntent.document_id == document_id),
            "parse_runs": count(ParseRun, ParseRun.id.in_(parses)),
            "pages": count(DocumentPage, DocumentPage.parse_run_id.in_(parses)),
            "elements": count(DocumentElement, DocumentElement.parse_run_id.in_(parses)),
            "parse_findings": count(
                ParseValidationFinding, ParseValidationFinding.parse_run_id.in_(parses)
            ),
            "chunk_runs": count(ChunkRun, ChunkRun.id.in_(chunk_runs)),
            "chunks": count(Chunk, Chunk.chunk_run_id.in_(chunk_runs)),
            "chunk_sources": count(
                ChunkSourceElement, ChunkSourceElement.chunk_run_id.in_(chunk_runs)
            )
            + count(ChunkSourcePage, ChunkSourcePage.chunk_run_id.in_(chunk_runs)),
            "chunk_findings": count(
                ChunkValidationFinding, ChunkValidationFinding.chunk_run_id.in_(chunk_runs)
            ),
            "embedding_runs": count(EmbeddingRun, EmbeddingRun.id.in_(embedding_runs)),
            "embeddings": count(
                ChunkEmbedding, ChunkEmbedding.embedding_run_id.in_(embedding_runs)
            ),
            "index_runs": count(IndexRun, IndexRun.document_id == document_id),
            "lexical_indexes": count(SparseIndex, SparseIndex.id.in_(sparse)),
            "lexical_rows": count(SparseDocument, SparseDocument.sparse_index_id.in_(sparse))
            + count(SparsePosting, SparsePosting.sparse_index_id.in_(sparse))
            + count(SparseTerm, SparseTerm.sparse_index_id.in_(sparse)),
        }


@pytest.fixture
def cited(stack, qdrant):  # noqa: F811
    """A retrieval-ready document cited by a verified answer, plus what is needed to inspect it."""
    client, control, credentials, body, _, _ = stack
    answered = ask(client, credentials)
    assert answered.status_code == 200 and answered.json()["outcome"] == "VERIFIED"
    document_id = UUID(body["document_id"])
    with control.sessions() as session:
        collections = sorted(
            set(
                session.scalars(
                    select(IndexRun.physical_collection).where(IndexRun.document_id == document_id)
                )
            )
        )
    return client, control, credentials, document_id, answered.json(), collections


# ------------------------------------------------------------------------------- who may delete


def test_a_reader_cannot_delete(cited):
    client, control, credentials, document_id, _, _ = cited
    assert delete(client, credentials, document_id, role=READER).status_code == 403
    assert owned_rows(control, document_id)["documents"] == 1


def test_a_curator_cannot_delete_either(cited):
    """Archiving is a curator's authority; destroying content is an administrator's."""
    _, control, credentials, document_id, _, _ = cited
    curator = Principal(
        user_id=credentials[ADMIN].user_id,
        tenant_id=credentials[ADMIN].tenant_id,
        display_name="Curator",
        role="curator",
    )
    with pytest.raises(DomainError) as raised:
        control.deletions.delete(curator, document_id, uuid4())
    assert raised.value.status == 403
    assert owned_rows(control, document_id)["documents"] == 1


def test_another_tenant_is_told_the_document_does_not_exist(cited):
    client, control, credentials, document_id, _, _ = cited
    response = delete(client, credentials, document_id, role=OTHER_TENANT)
    assert response.status_code == 404
    assert (
        client.get(
            f"/api/v1/documents/{document_id}/deletion-preview",
            headers=auth(credentials, OTHER_TENANT),
        ).status_code
        == 404
    )
    assert owned_rows(control, document_id)["documents"] == 1
    assert objects(control, document_id)


@pytest.mark.parametrize("body", [{"confirm": "delete"}, {"confirm": "yes"}, {"confirm": ""}])
def test_deletion_requires_the_exact_confirmation(cited, body):
    client, control, credentials, document_id, _, _ = cited
    response = delete(client, credentials, document_id, body=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DELETE_NOT_CONFIRMED"
    assert owned_rows(control, document_id)["documents"] == 1


def test_a_request_without_a_confirmation_is_rejected(cited):
    client, control, credentials, document_id, _, _ = cited
    assert delete(client, credentials, document_id, body=None).status_code == 422
    assert owned_rows(control, document_id)["documents"] == 1


def test_a_document_still_being_processed_cannot_be_deleted(chunked):  # noqa: F811
    """READY_FOR_EMBEDDING is mid-pipeline: a worker could still write after a cleanup."""
    client, control, credentials, body = chunked
    response = delete(client, credentials, body["document_id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DOCUMENT_PROCESSING"
    preview = client.get(
        f"/api/v1/documents/{body['document_id']}/deletion-preview", headers=auth(credentials)
    ).json()
    assert preview["blocked_reason"] == "PROCESSING"


# --------------------------------------------------------------------- what deletion removes


def test_the_preview_counts_what_will_be_removed(cited):
    client, _, credentials, document_id, _, _ = cited
    preview = client.get(
        f"/api/v1/documents/{document_id}/deletion-preview", headers=auth(credentials)
    )
    assert preview.status_code == 200
    data = preview.json()
    assert data["versions"] == 1 and data["chunks"] > 0 and data["vectors"] > 0
    assert data["citing_answers"] == 1 and data["blocked_reason"] is None
    assert (
        client.get(
            f"/api/v1/documents/{document_id}/deletion-preview", headers=auth(credentials, READER)
        ).status_code
        == 403
    )


def test_deletion_removes_the_document_from_every_store(cited, qdrant):  # noqa: F811
    client, control, credentials, document_id, _, collections = cited
    tenant = credentials[ADMIN].tenant_id
    before = owned_rows(control, document_id)
    assert all(before[key] for key in ("versions", "chunks", "embeddings", "lexical_rows"))
    assert objects(control, document_id), "the fixture must have stored objects"
    assert vectors(control, qdrant, collections, tenant, document_id) > 0

    response = delete(client, credentials, document_id)
    assert response.status_code == 204, response.text

    assert set(owned_rows(control, document_id).values()) == {0}
    # Versions and delete markers both: a versioned bucket keeps bytes behind a plain delete.
    assert objects(control, document_id) == []
    assert vectors(control, qdrant, collections, tenant, document_id) == 0


def test_a_deleted_document_is_gone_from_the_library_and_from_retrieval(cited):
    client, control, credentials, document_id, _, _ = cited
    assert delete(client, credentials, document_id).status_code == 204
    for archived in ("false", "true"):
        listed = client.get(
            f"/api/v1/documents?archived={archived}", headers=auth(credentials)
        ).json()
        assert str(document_id) not in {item["id"] for item in listed["items"]}
    assert (
        client.get(f"/api/v1/documents/{document_id}", headers=auth(credentials)).status_code == 404
    )
    actor = principal(control, credentials)
    # It was the tenant's only document, so nothing at all is left to search.
    with pytest.raises(RetrievalError) as raised:
        control.evidence.retrieval.search(actor, "synthetic table", uuid4())
    assert raised.value.code == "RETRIEVAL_CORPUS_EMPTY"


def test_another_document_in_the_same_tenant_is_untouched(cited):
    client, control, credentials, document_id, _, _ = cited
    neighbour, _ = queue_job(client, control, credentials, "basic-text.pdf")
    neighbour_id = UUID(neighbour["document_id"])
    before = owned_rows(control, neighbour_id)
    stored = objects(control, neighbour_id)
    assert delete(client, credentials, document_id).status_code == 204
    assert owned_rows(control, neighbour_id) == before
    assert len(objects(control, neighbour_id)) == len(stored)


# ------------------------------------------------------------------------- what deletion keeps


def test_a_past_answer_keeps_its_text_and_says_its_source_was_deleted(cited):
    client, control, credentials, document_id, answered, _ = cited
    assert delete(client, credentials, document_id).status_code == 204
    conversation = client.get(
        f"/api/v1/conversations/{answered['conversation_id']}", headers=auth(credentials)
    )
    assert conversation.status_code == 200
    turn = conversation.json()["turns"][-1]
    assert turn["outcome"] == "VERIFIED" and turn["answer"] == answered["answer"]
    assert turn["citations"]
    for citation in turn["citations"]:
        assert citation["source_deleted"] is True
        # None of the deleted content survives in the citation.
        assert citation["cited_text"] == ""
        assert citation["document_title"] == DELETED_TITLE
        assert citation["spans"] == [] and citation["pages"] == []
    assert turn["figures"] == []


def test_the_deletion_is_audited_and_tombstoned_without_any_content(cited):
    client, control, credentials, document_id, _, _ = cited
    with control.sessions() as session:
        title = session.scalar(select(Document.title).where(Document.id == document_id))
    assert delete(client, credentials, document_id).status_code == 204
    with control.sessions() as session:
        events = [
            row.event_type
            for row in session.scalars(
                select(AuditEvent)
                .where(AuditEvent.resource_id == document_id)
                .order_by(AuditEvent.created_at)
            )
        ]
        tombstone = session.scalar(
            select(DocumentDeletion).where(DocumentDeletion.document_id == document_id)
        )
        metadata = [
            row.safe_metadata
            for row in session.scalars(
                select(AuditEvent).where(AuditEvent.resource_id == document_id)
            )
        ]
    assert "DOCUMENT_DELETE_REQUESTED" in events and events[-1] == "DOCUMENT_DELETED"
    assert tombstone.status == "COMPLETED" and tombstone.completed_at is not None
    assert tombstone.removed["chunks"] > 0 and tombstone.removed["vectors"] > 0
    assert tombstone.requested_by_user_id == credentials[ADMIN].user_id
    # The tombstone and the audit trail name the document by id only.
    assert title not in str(tombstone.removed) and title not in str(metadata)


def test_deleting_again_is_a_harmless_no_op(cited):
    client, _, credentials, document_id, _, _ = cited
    assert delete(client, credentials, document_id).status_code == 204
    assert delete(client, credentials, document_id).status_code == 204


# ------------------------------------------------------------------------- partial failure


def test_a_failure_part_way_leaves_the_document_withdrawn_and_the_deletion_resumable(
    cited, monkeypatch
):
    client, control, credentials, document_id, _, _ = cited

    def broken(prefix):
        raise ConnectionError("object store unavailable")

    monkeypatch.setattr(control.deletions.storage, "delete_prefix", broken)
    response = delete(client, credentials, document_id)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DOCUMENT_DELETE_INCOMPLETE"
    # No storage detail leaks into what the user is told.
    assert "store" not in response.json()["error"]["message"].lower() or "unavailable" not in (
        response.json()["error"]["message"].lower()
    )

    # Withdrawn: not in the active library, not retrievable, and labelled for what it is.
    active = client.get("/api/v1/documents", headers=auth(credentials)).json()
    assert str(document_id) not in {item["id"] for item in active["items"]}
    with pytest.raises(RetrievalError):
        control.evidence.retrieval.search(principal(control, credentials), "synthetic", uuid4())
    lifecycle = client.get(
        f"/api/v1/documents/{document_id}/lifecycle", headers=auth(credentials)
    ).json()
    assert lifecycle["state"] == "DELETION_INCOMPLETE"
    with control.sessions() as session:
        assert (
            session.scalar(
                select(DocumentDeletion.status).where(DocumentDeletion.document_id == document_id)
            )
            == "FAILED"
        )
        assert session.scalar(
            select(func.count()).where(
                AuditEvent.resource_id == document_id,
                AuditEvent.event_type == "DOCUMENT_DELETE_FAILED",
            )
        )

    # Retrying once the store is back finishes the job.
    monkeypatch.undo()
    assert delete(client, credentials, document_id).status_code == 204
    assert set(owned_rows(control, document_id).values()) == {0}
    assert objects(control, document_id) == []


# ------------------------------------------------------------------- the guards still guard


def test_operational_history_is_still_append_only_outside_a_purge(cited):
    _, control, _, document_id, _, _ = cited
    with control.sessions.begin() as session:
        job = session.scalar(
            select(IngestionJob.id)
            .join(DocumentVersion, DocumentVersion.id == IngestionJob.document_version_id)
            .where(DocumentVersion.document_id == document_id)
        )
    with pytest.raises(Exception, match="append-only"), control.sessions.begin() as session:
        session.execute(
            text("DELETE FROM ingestion_stage_events WHERE ingestion_job_id = :job"), {"job": job}
        )


def test_a_completed_dataset_still_cannot_be_edited_outside_a_purge(cited):
    _, control, _, document_id, _, _ = cited
    with pytest.raises(Exception, match="cannot be changed"), control.sessions.begin() as session:
        session.execute(
            text(
                "DELETE FROM chunks WHERE chunk_run_id IN "
                "(SELECT id FROM chunk_runs WHERE document_id = :document)"
            ),
            {"document": document_id},
        )


def test_a_purge_of_one_document_cannot_reach_another(cited):
    client, control, credentials, document_id, _, _ = cited
    with control.sessions.begin() as session:
        job = session.scalar(
            select(IngestionJob.id)
            .join(DocumentVersion, DocumentVersion.id == IngestionJob.document_version_id)
            .where(DocumentVersion.document_id == document_id)
        )
    with pytest.raises(Exception, match="append-only"), control.sessions.begin() as session:
        session.execute(
            text("SELECT set_config('medrag.purge_document', :other, true)"),
            {"other": str(uuid4())},
        )
        session.execute(
            text("DELETE FROM ingestion_stage_events WHERE ingestion_job_id = :job"), {"job": job}
        )


def test_no_purge_can_remove_the_audit_trail(cited):
    _, control, _, document_id, _, _ = cited
    with pytest.raises(Exception, match="append-only"), control.sessions.begin() as session:
        session.execute(
            text("SELECT set_config('medrag.purge_document', :document, true)"),
            {"document": str(document_id)},
        )
        session.execute(text("DELETE FROM audit_events"))


def test_the_purge_setting_does_not_outlive_its_transaction(cited):
    _, control, _, document_id, _, _ = cited
    with control.sessions.begin() as session:
        session.execute(
            text("SELECT set_config('medrag.purge_document', :document, true)"),
            {"document": str(document_id)},
        )
    with control.sessions() as session:
        assert not session.scalar(
            text("SELECT medrag_purging(:document)"), {"document": document_id}
        )


# ------------------------------------------------------------------------- archive unchanged


def test_archiving_still_withdraws_without_destroying(cited):
    client, control, credentials, document_id, _, _ = cited
    before = owned_rows(control, document_id)
    archived = client.post(f"/api/v1/documents/{document_id}/archive", headers=auth(credentials))
    assert archived.status_code == 204
    after = owned_rows(control, document_id)
    # Every row is still there; archiving only changes state.
    assert after["chunks"] == before["chunks"] and after["embeddings"] == before["embeddings"]
    assert objects(control, document_id)
    with control.sessions() as session:
        assert (
            session.scalar(select(func.count()).where(DocumentDeletion.document_id == document_id))
            == 0
        )
