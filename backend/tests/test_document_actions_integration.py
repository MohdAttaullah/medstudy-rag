"""Version and delete actions follow the document's lifecycle, on the server.

The interface hides "Add a new file version" and "Delete permanently" while a document is being
processed. That is presentation only: these tests prove the server refuses both from a stale tab,
and allows both again once the pipeline has stopped — including at NEEDS_REVIEW, which is a paused
state, not active work.
"""

import os
from uuid import UUID, uuid4

import pytest
from app.ingestion.state import transition
from app.models.documents import DocumentVersion, IngestionJob
from app.models.enums import Status
from tests.test_m1_integration import FIXTURES, auth, database, upload  # noqa: F401 - fixtures
from tests.test_m2_integration import system_module as system_module
from tests.test_m4_integration import chunked, qdrant  # noqa: F401, F811
from tests.test_m5_integration import build_sparse, indexed  # noqa: F401, F811

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

ADMIN, READER, OTHER_TENANT = 0, 1, 2
CONFIRM = {"confirm": "DELETE"}


def fresh():
    """A valid PDF no other test has uploaded, so duplicate detection never interferes."""
    return (FIXTURES / "edition-two.pdf").read_bytes() + f"\n% {uuid4()}\n".encode()


def lifecycle(client, credentials, document_id):
    return client.get(
        f"/api/v1/documents/{document_id}/lifecycle", headers=auth(credentials)
    ).json()


def delete(client, credentials, document_id, role=ADMIN):
    return client.request(
        "DELETE", f"/api/v1/documents/{document_id}", json=CONFIRM, headers=auth(credentials, role)
    )


def test_mid_pipeline_refuses_a_new_version_and_deletion(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    # Chunked and waiting for embedding: the lifecycle says the document is processing.
    assert lifecycle(client, credentials, body["document_id"])["state"] == "PROCESSING"
    version = upload(
        client,
        credentials,
        file="edition-two.pdf",
        document_id=body["document_id"],
        content=fresh(),
    )
    assert version.status_code == 409 and version.json()["error"]["code"] == "DOCUMENT_PROCESSING"
    refused = delete(client, credentials, body["document_id"])
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "DOCUMENT_PROCESSING"
    versions = client.get(
        f"/api/v1/documents/{body['document_id']}/versions", headers=auth(credentials)
    ).json()
    assert versions["total"] == 1


def test_a_paused_review_is_stable_and_allows_both(chunked):  # noqa: F811
    client, control, credentials, body = chunked
    with control.sessions.begin() as session:
        job = session.get(IngestionJob, UUID(body["job_id"]))
        # Reached through the state machine, as the pipeline reaches it.
        version = session.get(DocumentVersion, job.document_version_id)
        transition(
            session, job, version, Status.NEEDS_REVIEW, None, error_code="CHUNK_NEEDS_REVIEW"
        )
    assert lifecycle(client, credentials, body["document_id"])["state"] == "REVIEW_REQUIRED"
    added = upload(
        client,
        credentials,
        file="edition-two.pdf",
        document_id=body["document_id"],
        content=fresh(),
    )
    assert added.status_code == 201, added.text


def test_a_ready_document_allows_a_new_version(indexed):  # noqa: F811
    client, _, credentials, body, _, _ = indexed
    build_sparse(indexed)
    assert lifecycle(client, credentials, body["document_id"])["state"] == "READY"
    added = upload(
        client,
        credentials,
        file="edition-two.pdf",
        document_id=body["document_id"],
        content=fresh(),
    )
    assert added.status_code == 201, added.text


def test_a_failed_document_allows_deletion(chunked):  # noqa: F811
    client, control, credentials, body = chunked
    with control.sessions.begin() as session:
        job = session.get(IngestionJob, UUID(body["job_id"]))
        version = session.get(DocumentVersion, job.document_version_id)
        transition(session, job, version, Status.FAILED, None, error_code="EMBEDDING_FAILED")
    assert lifecycle(client, credentials, body["document_id"])["state"] == "FAILED"
    assert delete(client, credentials, body["document_id"]).status_code == 204


def test_permissions_and_tenancy_are_unchanged(indexed):  # noqa: F811
    client, _, credentials, body, _, _ = indexed
    build_sparse(indexed)
    reader = upload(
        client,
        credentials,
        file="edition-two.pdf",
        document_id=body["document_id"],
        index=READER,
        content=fresh(),
    )
    assert reader.status_code == 403
    other = upload(
        client,
        credentials,
        file="edition-two.pdf",
        document_id=body["document_id"],
        index=OTHER_TENANT,
        content=fresh(),
    )
    assert other.status_code == 404
    assert delete(client, credentials, body["document_id"], READER).status_code == 403
    assert delete(client, credentials, body["document_id"], OTHER_TENANT).status_code == 404
