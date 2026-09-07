"""Real PostgreSQL, HTTP authorization, immutable history and tenant snapshots."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from app.models.configuration import ConfigurationRevision
from app.models.documents import AuditEvent
from app.services.configuration import ConfigurationService
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from tests.test_m1_integration import auth, database  # noqa: F401
from tests.test_m1_integration import system as system

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires PostgreSQL"
    ),
]


def preview(client, credentials, key="retrieval.rrf_k", value=50, revision=0, role=0):
    return client.post(
        "/api/v1/settings/preview",
        headers=auth(credentials, role),
        json={"expected_revision": revision, "changes": [{"key": key, "value": value}]},
    )


def apply(client, credentials, prepared, role=0, **extra):
    p = prepared.json()
    return client.post(
        "/api/v1/settings/changes",
        headers=auth(credentials, role),
        json={
            "expected_revision": p["revision"],
            "changes": [{"key": c["key"], "value": c["new_value"]} for c in p["changes"]],
            "preview_token": p["preview_token"],
            "confirmed": True,
            **extra,
        },
    )


def test_read_auth_secrets_and_all_sections(system):
    client, control, credentials = system
    assert client.get("/api/v1/settings").status_code == 401
    assert client.get("/api/v1/settings", headers=auth(credentials, 1)).status_code == 403
    result = client.get("/api/v1/settings", headers=auth(credentials))
    assert result.status_code == 200, result.text
    assert len({v["section"] for v in result.json()["settings"]}) == 10
    for credential in credentials:
        assert credential.token.get_secret_value() not in result.text
    assert control.settings.database_url.get_secret_value() not in result.text
    assert "api_key" not in result.text and "base_url" not in result.text


def test_preview_has_no_mutation_and_apply_has_history(system):
    client, control, credentials = system
    prepared = preview(client, credentials)
    assert prepared.status_code == 200, prepared.text
    assert client.get("/api/v1/settings", headers=auth(credentials)).json()["revision"] == 0
    response = apply(client, credentials, prepared, reason="Measured policy change")
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1 and response.json()["result"] == "ACTIVE"
    actor = control.auth.authenticate(auth(credentials)["Authorization"])
    restarted = ConfigurationService(control.sessions, control.settings)
    effective, snapshot = restarted.resolve(actor)
    assert effective.retrieval.rrf_k == 50 and snapshot["revision"] == 1
    assert control.settings.retrieval.rrf_k == 60
    history = client.get("/api/v1/settings/history", headers=auth(credentials)).json()
    assert history[0]["changes"][0]["old_value"] == 60
    assert history[0]["actor_id"] == str(credentials[0].user_id)
    with control.sessions() as session:
        audit = session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.event_type == "CONFIGURATION_ACTIVE",
            )
        ).one()
        assert audit.safe_metadata["changes"][0]["new_value"] == 50


def test_reader_foreign_preview_and_global_write_denied(system):
    client, _, credentials = system
    prepared = preview(client, credentials)
    assert apply(client, credentials, prepared, role=1).status_code == 403
    assert apply(client, credentials, prepared, role=2).status_code == 409
    assert preview(client, credentials, "reranker.batch_size", 4).status_code == 422
    assert client.get("/api/v1/settings/history", headers=auth(credentials, 2)).json() == []


def test_pending_rebuild_never_mutates_active_configuration(system):
    client, control, credentials = system
    prepared = preview(client, credentials, "chunking.child_target_tokens", 320)
    assert prepared.status_code == 200, prepared.text
    result = apply(client, credentials, prepared)
    assert result.json()["result"] == "PENDING_REBUILD"
    values = {
        v["key"]: v
        for v in client.get("/api/v1/settings", headers=auth(credentials)).json()["settings"]
    }
    item = values["chunking.child_target_tokens"]
    assert (
        item["effective_value"] == 384
        and item["desired_value"] == 320
        and item["status"] == "PENDING_REBUILD"
    )
    actor = control.auth.authenticate(auth(credentials)["Authorization"])
    scoped = control.for_request(actor)
    assert scoped.settings.chunking == control.settings.chunking
    assert scoped.settings.embedding == control.settings.embedding
    assert scoped.settings.sparse_analyzer == control.settings.sparse_analyzer


def test_two_first_writers_cannot_overwrite_and_old_snapshot_is_stable(system):
    client, control, credentials = system
    prepared = preview(client, credentials)
    actor = control.auth.authenticate(auth(credentials)["Authorization"])
    before = control.for_request(actor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: apply(client, credentials, prepared).status_code, range(2))
        )
    assert sorted(results) == [200, 409]
    after = control.for_request(actor)
    assert before.retrieval.config.rrf_k == 60 and after.retrieval.config.rrf_k == 50
    assert (
        after.evidence.settings.retrieval.rrf_k
        == after.generation.settings.retrieval.rrf_k
        == after.verification.settings.retrieval.rrf_k
        == 50
    )
    assert before.ask.configuration_snapshot["revision"] == 0
    assert after.ask.configuration_snapshot["revision"] == 1


def test_preview_change_tampering_and_missing_confirmation_rejected(system):
    client, _, credentials = system
    prepared = preview(client, credentials)
    assert apply(client, credentials, prepared, confirmed=False).status_code == 422
    assert (
        apply(
            client, credentials, prepared, changes=[{"key": "retrieval.rrf_k", "value": 70}]
        ).status_code
        == 409
    )
    assert client.get("/api/v1/settings", headers=auth(credentials)).json()["revision"] == 0


def test_immutable_history_and_unique_revision_database_constraints(system):
    client, control, credentials = system
    assert apply(client, credentials, preview(client, credentials)).status_code == 200
    with control.sessions() as session:
        row = session.scalars(
            select(ConfigurationRevision).where(
                ConfigurationRevision.tenant_id == credentials[0].tenant_id
            )
        ).one()
        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE configuration_revisions SET reason = 'overwrite' WHERE id = :id"),
                {"id": row.id},
            )
            session.flush()
        session.rollback()
        with pytest.raises(DBAPIError):
            session.execute(
                text("DELETE FROM configuration_revisions WHERE tenant_id = :tenant"),
                {"tenant": credentials[0].tenant_id},
            )
        session.rollback()
        row = session.scalars(
            select(ConfigurationRevision).where(
                ConfigurationRevision.tenant_id == credentials[0].tenant_id
            )
        ).one()
        duplicate = {
            column.name: getattr(row, column.name)
            for column in ConfigurationRevision.__table__.columns
        }
        duplicate["id"] = uuid4()
        with pytest.raises(DBAPIError):
            session.execute(ConfigurationRevision.__table__.insert().values(**duplicate))
        session.rollback()


def test_rejected_values_are_not_audit_payloads(system, caplog):
    client, control, credentials = system
    secret = "sk-" + uuid4().hex + uuid4().hex
    result = client.post(
        "/api/v1/settings/changes",
        headers=auth(credentials),
        json={
            "expected_revision": 0,
            "changes": [{"key": "openai_api_key", "value": secret}],
            "preview_token": "0" * 64,
            "confirmed": True,
        },
    )
    assert result.status_code == 422 and secret not in result.text
    with control.sessions() as session:
        rows = session.scalars(
            select(AuditEvent).where(AuditEvent.tenant_id == credentials[0].tenant_id)
        ).all()
        assert any(row.event_type == "CONFIGURATION_REJECTED" for row in rows)
        assert secret not in json.dumps([row.safe_metadata for row in rows])
    assert secret not in caplog.text


def test_no_secret_in_change_reason_and_mixed_activation_is_atomic(system):
    client, control, credentials = system
    prepared = preview(client, credentials)
    assert (
        apply(
            client, credentials, prepared, reason=credentials[0].token.get_secret_value()
        ).status_code
        == 422
    )
    response = client.post(
        "/api/v1/settings/preview",
        headers=auth(credentials),
        json={
            "expected_revision": 0,
            "changes": [
                {"key": "retrieval.rrf_k", "value": 50},
                {"key": "chunking.child_target_tokens", "value": 320},
            ],
        },
    )
    assert response.status_code == 422
    assert client.get("/api/v1/settings", headers=auth(credentials)).json()["revision"] == 0


def test_downgrade_refuses_historical_policy_loss(system):
    from alembic import command
    from alembic.config import Config

    client, _, credentials = system
    assert apply(client, credentials, preview(client, credentials)).status_code == 200
    assert os.environ.get("MEDRAG_ALLOW_CONFIGURATION_LOSS") != "1"
    with pytest.raises(RuntimeError, match="history exists"):
        command.downgrade(Config("alembic.ini"), "m9_conversations")
    assert (
        client.get("/api/v1/settings/history", headers=auth(credentials)).json()[0]["revision"] == 1
    )


def test_policy_snapshots_reuse_lazy_encoder_and_audit_invalid_bodies(system):
    client, control, credentials = system
    assert client.get("/api/v1/auth/me", headers=auth(credentials)).status_code == 200
    actor = control.auth.authenticate(auth(credentials)["Authorization"])
    calls = []
    encoder = object()

    def factory():
        calls.append(1)
        return encoder

    control.retrieval._encoder = None
    control.retrieval._encoder_factory = factory
    assert control.for_request(actor).retrieval.encoder() is encoder
    assert control.for_request(actor).retrieval.encoder() is encoder
    assert calls == [1]
    invalid = client.post(
        "/api/v1/settings/changes", headers=auth(credentials), json={"tenant_id": str(uuid4())}
    )
    assert invalid.status_code == 422
    with control.sessions() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.event_type == "CONFIGURATION_REJECTED",
            )
        ).all()
        assert events[-1].safe_metadata == {"result": "INVALID_REQUEST", "scope": "TENANT"}
