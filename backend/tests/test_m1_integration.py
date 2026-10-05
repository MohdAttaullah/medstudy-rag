import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.db.session import make_engine
from app.main import create_app
from app.models.documents import (
    AuditEvent,
    Document,
    DocumentVersion,
    IngestionJob,
    OutboxMessage,
    UploadIntent,
)
from app.models.enums import Status
from app.security.auth import DevCredential
from app.services.queue import dispatch, receive
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires PostgreSQL and MinIO"
    ),
]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def database():
    settings = Settings(_env_file=".env")
    admin = make_engine(settings)
    schema = "m1_test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(settings.database_url.get_secret_value()).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    value = url.render_as_string(hide_password=False)
    previous = os.environ.get("MEDRAG_DATABASE_URL")
    os.environ["MEDRAG_DATABASE_URL"] = value
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        command.check(config)
        yield value
        # Exercise reversible migration on this isolated test schema only. The M9 downgrade guard
        # protects real question history, so this throwaway schema acknowledges the loss.
        os.environ["MEDRAG_ALLOW_CONVERSATION_LOSS"] = "1"
        os.environ["MEDRAG_ALLOW_CONFIGURATION_LOSS"] = "1"
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    finally:
        os.environ.pop("MEDRAG_ALLOW_CONVERSATION_LOSS", None)
        os.environ.pop("MEDRAG_ALLOW_CONFIGURATION_LOSS", None)
        if previous is None:
            os.environ.pop("MEDRAG_DATABASE_URL", None)
        else:
            os.environ["MEDRAG_DATABASE_URL"] = previous
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def system(database):
    tenant = uuid4()
    credentials = (
        DevCredential(
            token=uuid4().hex,
            user_id=uuid4(),
            tenant_id=tenant,
            display_name="Curator",
            role="admin",
        ),
        DevCredential(
            token=uuid4().hex,
            user_id=uuid4(),
            tenant_id=tenant,
            display_name="Reader",
            role="reader",
        ),
        DevCredential(
            token=uuid4().hex,
            user_id=uuid4(),
            tenant_id=uuid4(),
            display_name="Other tenant",
            role="admin",
        ),
    )
    settings = Settings(_env_file=".env", database_url=database, dev_principals=credentials)
    app = create_app(settings)
    control = app.state.control
    with TestClient(app) as client:
        yield client, control, credentials
    # Cleanup only UUID-addressed objects created by this test tenant, after test completion.
    with control.sessions() as session:
        keys = list(
            session.scalars(
                select(UploadIntent.object_key).where(
                    UploadIntent.tenant_id.in_([tenant, credentials[2].tenant_id])
                )
            )
        )
    for key in keys:
        control.storage.delete(key)


def auth(credentials, index=0):
    return {"Authorization": "Bearer " + credentials[index].token.get_secret_value()}


def upload(
    client,
    credentials,
    *,
    file="valid.pdf",
    key=None,
    index=0,
    document_id=None,
    metadata=None,
    content=None,
):
    meta = metadata or {"filename": file, "edition": "First", "publication_year": 2025}
    if document_id is None and "document" not in meta:
        meta = {
            **meta,
            "document": {
                "title": "Synthetic reference",
                "source_type": "TEXTBOOK",
                "authority_level": "UNREVIEWED",
            },
        }
    headers = {
        **auth(credentials, index),
        "Content-Type": "application/pdf",
        "Idempotency-Key": str(key or uuid4()),
        "X-Upload-Metadata": base64.b64encode(json.dumps(meta).encode()).decode(),
    }
    return client.post(
        f"/api/v1/documents/{document_id}/versions" if document_id else "/api/v1/documents",
        headers=headers,
        content=(FIXTURES / file).read_bytes() if content is None else content,
    )


def test_upload_persistence_history_storage_and_lists(system):
    client, control, credentials = system
    response = upload(client, credentials)
    assert response.status_code == 201, response.text
    ids = response.json()
    assert ids["status"] == "QUEUED"
    headers = auth(credentials)
    detail = client.get(f"/api/v1/documents/{ids['document_id']}", headers=headers).json()
    version = detail["latest_version"]
    assert version["sha256"] == hashlib.sha256((FIXTURES / "valid.pdf").read_bytes()).hexdigest()
    assert version["searchable"] is False and version["page_count"] is None
    assert "object_storage_key" not in version
    job = client.get(f"/api/v1/ingestion/jobs/{ids['job_id']}", headers=headers).json()
    assert [event["to_status"] for event in job["events"]] == ["UPLOADED", "VALIDATING", "QUEUED"]
    assert job["config_snapshot"]["duplicate_policy"] == "reject-within-tenant"
    source = client.get(
        f"/api/v1/documents/{ids['document_id']}/versions/{ids['version_id']}/source",
        headers=headers,
    )
    assert source.content == (FIXTURES / "valid.pdf").read_bytes()
    assert "attachment" in source.headers["Content-Disposition"]
    assert client.get("/api/v1/documents?limit=1", headers=headers).json()["total"] == 1
    assert client.get("/api/v1/ingestion/jobs?status=QUEUED", headers=headers).json()["total"] == 1
    events = client.get("/api/v1/audit?limit=100", headers=headers).json()["items"]
    assert {"DOCUMENT_CREATED", "DOCUMENT_VERSION_UPLOADED", "INGESTION_JOB_CREATED"} <= {
        item["event_type"] for item in events
    }
    with control.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .join(IngestionJob)
                .where(IngestionJob.tenant_id == credentials[0].tenant_id)
            )
            == 1
        )


def test_idempotency_duplicate_new_version_and_scope(system):
    client, control, credentials = system
    key = uuid4()
    first = upload(client, credentials, key=key).json()
    replay = upload(client, credentials, key=key)
    assert replay.status_code == 201
    assert replay.json() == {**first, "replayed": True}
    conflict = upload(client, credentials, key=key, file="edition-two.pdf")
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    duplicate = upload(client, credentials, file="duplicate.pdf")
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "UPLOAD_DUPLICATE"
    # A new version is refused while the current one is still being processed — here it is still
    # queued — and accepted once that job has stopped.
    early = upload(client, credentials, file="edition-two.pdf", document_id=first["document_id"])
    assert early.status_code == 409, early.text
    assert early.json()["error"]["code"] == "DOCUMENT_PROCESSING"
    cancelled = client.post(
        f"/api/v1/ingestion/jobs/{first['job_id']}/cancel", headers=auth(credentials)
    )
    assert cancelled.status_code == 200, cancelled.text
    second = upload(client, credentials, file="edition-two.pdf", document_id=first["document_id"])
    assert second.status_code == 201, second.text
    versions = client.get(
        f"/api/v1/documents/{first['document_id']}/versions", headers=auth(credentials)
    ).json()
    assert [item["version_number"] for item in versions["items"]] == [2, 1]
    # Identical bytes in another tenant are not disclosed as duplicates.
    assert upload(client, credentials, index=2).status_code == 201
    assert (
        client.get(
            f"/api/v1/documents/{first['document_id']}", headers=auth(credentials, 2)
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/ingestion/jobs/{first['job_id']}", headers=auth(credentials, 2)
        ).status_code
        == 404
    )
    assert (
        upload(
            client, credentials, file="edition-two.pdf", index=2, document_id=first["document_id"]
        ).status_code
        == 404
    )


def test_unauthorized_and_reader_cannot_mutate(system):
    client, control, credentials = system
    assert client.post("/api/v1/documents", content=b"private").status_code == 401
    denied = upload(client, credentials, index=1)
    assert denied.status_code == 403
    events = client.get("/api/v1/audit", headers=auth(credentials)).json()["items"]
    assert any(
        item["event_type"] == "UPLOAD_REJECTED"
        and item["correlation_id"] == denied.headers["X-Request-ID"]
        for item in events
    )
    ids = upload(client, credentials).json()
    for suffix in ("cancel", "retry"):
        assert (
            client.post(
                f"/api/v1/ingestion/jobs/{ids['job_id']}/{suffix}", headers=auth(credentials, 1)
            ).status_code
            == 403
        )
    assert (
        client.post(
            f"/api/v1/documents/{ids['document_id']}/archive", headers=auth(credentials, 1)
        ).status_code
        == 403
    )
    assert client.get("/api/v1/documents", headers=auth(credentials, 1)).status_code == 200
    assert client.get("/api/v1/audit", headers=auth(credentials, 1)).status_code == 403


@pytest.mark.parametrize(
    "file,code",
    [
        ("malformed.pdf", "UPLOAD_INVALID_PDF"),
        ("empty.pdf", "UPLOAD_EMPTY"),
        ("wrong-content.pdf", "UPLOAD_INVALID_PDF"),
        ("wrong-extension.txt", "UPLOAD_UNSUPPORTED_TYPE"),
    ],
)
def test_invalid_uploads_are_audited_without_documents(system, file, code):
    client, control, credentials = system
    response = upload(client, credentials, file=file)
    assert response.status_code in (400, 415)
    assert response.json()["error"]["code"] == code
    assert client.get("/api/v1/documents", headers=auth(credentials)).json()["total"] == 0
    events = client.get("/api/v1/audit", headers=auth(credentials)).json()["items"]
    assert any(item["event_type"] == "UPLOAD_REJECTED" for item in events)


def test_invalid_metadata_and_pagination(system):
    client, control, credentials = system
    meta = {"filename": "valid.pdf", "document": {"title": "", "source_type": "TEXTBOOK"}}
    assert upload(client, credentials, metadata=meta).status_code == 422
    for path in ("/api/v1/documents?limit=0", "/api/v1/ingestion/jobs?offset=-1"):
        assert client.get(path, headers=auth(credentials)).status_code == 422


def test_bounded_chunked_upload(system):
    client, control, credentials = system
    settings = control.settings.model_copy(
        update={
            "ingestion": control.settings.ingestion.model_copy(update={"max_upload_bytes": 1024})
        }
    )
    app = create_app(settings)
    metadata = {
        "filename": "large.pdf",
        "document": {
            "title": "Size test",
            "source_type": "TEXTBOOK",
            "authority_level": "UNREVIEWED",
        },
    }
    headers = {
        **auth(credentials),
        "Content-Type": "application/pdf",
        "Idempotency-Key": str(uuid4()),
        "X-Upload-Metadata": base64.b64encode(json.dumps(metadata).encode()).decode(),
    }
    with TestClient(app) as bounded:
        response = bounded.post(
            "/api/v1/documents", headers=headers, content=iter([b"%PDF-" + b"x" * 600, b"x" * 600])
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "UPLOAD_FILE_TOO_LARGE"


def test_storage_failure_compensation_and_recovery(system, monkeypatch):
    client, control, credentials = system
    original_put, original_delete = control.storage.put, control.storage.delete

    def write_then_fail(*args):
        original_put(*args)
        raise RuntimeError("secret-provider-diagnostic")

    def broken_delete(key):
        raise RuntimeError("secret-storage-diagnostic")

    monkeypatch.setattr(control.storage, "put", write_then_fail)
    monkeypatch.setattr(control.storage, "delete", broken_delete)
    response = upload(client, credentials)
    assert response.status_code == 503 and "secret" not in response.text
    assert response.json()["error"]["details"]["retry_with_new_key"] == "true"
    with control.sessions() as session:
        intent = session.scalar(
            select(UploadIntent).where(UploadIntent.tenant_id == credentials[0].tenant_id)
        )
        assert intent.state == "FAILED" and intent.cleanup_required
        object_key = intent.object_key
        assert (
            session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.tenant_id == credentials[0].tenant_id)
            )
            == 0
        )
    monkeypatch.setattr(control.storage, "delete", original_delete)
    assert control.uploads.recover() >= 1
    assert not control.storage.exists(object_key)


def test_database_failure_after_storage_compensates(system, monkeypatch):
    import app.services.uploads as upload_module

    client, control, credentials = system

    def fail_transition(*args, **kwargs):
        raise RuntimeError("database-failure-simulation")

    monkeypatch.setattr(upload_module, "transition", fail_transition)
    response = upload(client, credentials)
    assert response.status_code == 503
    with control.sessions() as session:
        intent = session.scalar(
            select(UploadIntent).where(UploadIntent.tenant_id == credentials[0].tenant_id)
        )
        assert intent.state == "FAILED" and not intent.cleanup_required
        assert session.get(DocumentVersion, intent.version_id) is None
        assert not control.storage.exists(intent.object_key)


def test_dispatch_receipt_retry_cancel_and_stale_delivery(system, monkeypatch):
    client, control, credentials = system
    ids = upload(client, credentials).json()

    class Publisher:
        messages = []

        def publish(self, message_id):
            self.messages.append(message_id)

    publisher = Publisher()
    assert dispatch(control.sessions, publisher, control.settings.ingestion) >= 1
    with control.sessions() as session:
        message_id = session.scalar(
            select(OutboxMessage.id).where(OutboxMessage.job_id == UUID(ids["job_id"]))
        )
    # Missing original at receipt makes a real retryable failure.
    original_stat = control.storage.stat

    def unavailable(*args):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(control.storage, "stat", unavailable)
    receive(control.sessions, control.storage, message_id)
    job_url = f"/api/v1/ingestion/jobs/{ids['job_id']}"
    assert client.get(job_url, headers=auth(credentials)).json()["status"] == "FAILED"
    monkeypatch.setattr(control.storage, "stat", original_stat)
    result = client.post(job_url + "/retry", headers=auth(credentials))
    assert result.status_code == 200 and result.json()["status"] == "QUEUED"
    assert result.json()["retry_count"] == 1
    receive(control.sessions, control.storage, message_id)  # Stale generation is harmless.
    assert (
        client.post(job_url + "/cancel", headers=auth(credentials)).json()["status"] == "CANCELLED"
    )
    count = len(client.get(job_url, headers=auth(credentials)).json()["events"])
    assert client.post(job_url + "/cancel", headers=auth(credentials)).status_code == 200
    assert len(client.get(job_url, headers=auth(credentials)).json()["events"]) == count
    assert client.post(job_url + "/retry", headers=auth(credentials)).status_code == 409
    with control.sessions() as session:
        messages = list(
            session.scalars(
                select(OutboxMessage.id).where(OutboxMessage.job_id == UUID(ids["job_id"]))
            )
        )
    for message in messages:
        receive(control.sessions, control.storage, message)
    assert client.get(job_url, headers=auth(credentials)).json()["status"] == "CANCELLED"


def test_receipt_idempotence_archive_and_immutability(system):
    client, control, credentials = system
    ids = upload(client, credentials).json()
    with control.sessions() as session:
        message_id = session.scalar(
            select(OutboxMessage.id).where(OutboxMessage.job_id == UUID(ids["job_id"]))
        )
    receive(control.sessions, control.storage, message_id)
    receive(control.sessions, control.storage, message_id)
    with control.sessions() as session:
        job = session.get(IngestionJob, UUID(ids["job_id"]))
        assert job.status == Status.QUEUED and job.queue_received_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.resource_id == job.id,
                    AuditEvent.event_type == "INGESTION_QUEUE_RECEIVED",
                )
            )
            == 1
        )
    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        session.execute(
            text("UPDATE document_versions SET sha256 = :sha WHERE id = :id"),
            {"sha": "0" * 64, "id": ids["version_id"]},
        )
    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        session.execute(
            text("UPDATE ingestion_jobs SET status = 'READY' WHERE id = :id"), {"id": ids["job_id"]}
        )
    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        session.execute(
            text("DELETE FROM ingestion_stage_events WHERE ingestion_job_id = :id"),
            {"id": ids["job_id"]},
        )
    assert (
        client.post(
            f"/api/v1/documents/{ids['document_id']}/archive", headers=auth(credentials)
        ).status_code
        == 204
    )
    assert client.get("/api/v1/documents", headers=auth(credentials)).json()["total"] == 0
    assert (
        client.get("/api/v1/documents?archived=true", headers=auth(credentials)).json()["total"]
        == 1
    )
    assert (
        upload(
            client, credentials, file="edition-two.pdf", document_id=ids["document_id"]
        ).status_code
        == 409
    )


def test_concurrent_same_key_creates_one_version(system):
    client, control, credentials = system
    key = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(client, credentials, key=key), range(2)))
    assert any(response.status_code == 201 for response in responses)
    assert all(response.status_code in (201, 409) for response in responses)
    with control.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DocumentVersion)
                .where(DocumentVersion.tenant_id == credentials[0].tenant_id)
            )
            == 1
        )
    assert upload(client, credentials, key=key).json()["replayed"]


def test_broker_failure_remains_durable(system):
    client, control, credentials = system
    ids = upload(client, credentials).json()

    class UnavailablePublisher:
        def publish(self, message_id):
            raise RuntimeError("broker unavailable")

    dispatch(control.sessions, UnavailablePublisher(), control.settings.ingestion)
    with control.sessions() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.job_id == UUID(ids["job_id"]))
        )
        assert message.last_error_code == "QUEUE_UNAVAILABLE" and message.received_at is None
        assert session.get(IngestionJob, UUID(ids["job_id"])).status == Status.QUEUED
