"""The lifecycle endpoint over the real pipeline: one document mid-pipeline, one ready for Ask.

The processing screen is only as honest as this response, so what is asserted is that it is
reconstructed from persisted state — the same answer after a reload, for every viewer — that it
never claims a stage it has not reached, and that it offers a reader nothing they cannot do.
"""

import os
from uuid import UUID

import pytest
from tests.test_m1_integration import auth, database  # noqa: F401 - fixtures
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


def lifecycle(client, credentials, document_id, role=ADMIN):
    return client.get(f"/api/v1/documents/{document_id}/lifecycle", headers=auth(credentials, role))


def stages(view):
    return {stage["code"]: stage for stage in view["stages"]}


def test_a_document_mid_pipeline_reports_the_stage_it_is_in(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    view = lifecycle(client, credentials, body["document_id"]).json()
    assert view["state"] == "PROCESSING" and view["terminal"] is False
    assert view["current_stage"] == "EMBEDDING"
    # Waiting for an embedding worker, and saying so rather than claiming to embed.
    assert view["activity"] == "READY_FOR_EMBEDDING"
    by_code = stages(view)
    assert by_code["READING"]["state"] == "COMPLETED"
    assert by_code["PASSAGES"]["state"] == "COMPLETED"
    assert by_code["PASSAGES"]["facts"]["passages"] > 0
    assert by_code["EMBEDDING"]["state"] == "RUNNING"
    for later in ("SEMANTIC_INDEX", "KEYWORD_INDEX", "READY"):
        assert by_code[later]["state"] == "PENDING" and by_code[later]["facts"] == {}
    assert view["run_started_at"] and view["server_time"]
    # No estimate is invented for a stage the history cannot speak for.
    assert view["estimate"]["available"] is False


def test_the_same_state_is_reconstructed_on_every_read(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    first = lifecycle(client, credentials, body["document_id"]).json()
    second = lifecycle(client, credentials, body["document_id"]).json()
    for key in ("state", "current_stage", "activity", "run_started_at"):
        assert first[key] == second[key]
    assert first["stages"] == second["stages"]


def test_a_ready_document_reports_every_stage_completed_with_real_counts(indexed):  # noqa: F811
    client, _, credentials, body, _, _ = indexed
    build_sparse(indexed)
    view = lifecycle(client, credentials, body["document_id"]).json()
    assert view["state"] == "READY" and view["terminal"] is True
    assert {stage["state"] for stage in view["stages"]} == {"COMPLETED"}
    by_code = stages(view)
    semantic = by_code["SEMANTIC_INDEX"]["facts"]
    assert semantic["verified_vectors"] == semantic["expected_vectors"] > 0
    assert by_code["KEYWORD_INDEX"]["facts"]["verified_passages"] > 0
    assert view["finished_at"] and view["review"] is None and view["failure"] is None


def test_another_tenant_is_told_the_document_does_not_exist(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    assert lifecycle(client, credentials, body["document_id"], OTHER_TENANT).status_code == 404


def test_a_reader_sees_the_state_but_is_offered_no_action(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    reader = lifecycle(client, credentials, body["document_id"], READER)
    assert reader.status_code == 200
    assert reader.json()["state"] == "PROCESSING"
    assert reader.json()["actions"] == []
    curator = lifecycle(client, credentials, body["document_id"]).json()
    assert {item["action"] for item in curator["actions"]} >= {"cancel", "reparse"}


def test_the_library_row_carries_a_compact_lifecycle(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    listed = client.get("/api/v1/documents", headers=auth(credentials)).json()
    row = next(item for item in listed["items"] if item["id"] == body["document_id"])
    assert row["lifecycle"]["state"] == "PROCESSING"
    assert row["lifecycle"]["current_stage"] == "EMBEDDING"
    detail = client.get(f"/api/v1/documents/{body['document_id']}", headers=auth(credentials))
    assert detail.json()["lifecycle"]["state"] == "PROCESSING"
    assert UUID(detail.json()["id"])
