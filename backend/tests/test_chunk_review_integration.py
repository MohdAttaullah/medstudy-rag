"""The Chunk Inspector's review data, over a real parse and chunk run.

Whatever findings a run has, the inspector must be able to say, without guessing: how many chunks
block, warn or are clean; list exactly those; and show each chunk's own findings beside it. Every
chunk falls in exactly one of the three, under its worst finding.
"""

import os

import pytest
from tests.test_m1_integration import auth, database  # noqa: F401 - fixtures
from tests.test_m2_integration import system_module as system_module
from tests.test_m4_integration import chunked, qdrant  # noqa: F401, F811

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires local infrastructure"
    ),
]

ADMIN, READER, OTHER_TENANT = 0, 1, 2
BLOCKING = {"CRITICAL", "ERROR"}


def run_id(client, credentials, body):
    runs = client.get(
        f"/api/v1/documents/{body['document_id']}/versions/{body['version_id']}/chunk-runs",
        headers=auth(credentials),
    ).json()["items"]
    return runs[0]["id"]


def listed(client, credentials, run, status=None, role=ADMIN):
    query = "?limit=100" + (f"&status={status}" if status else "")
    response = client.get(
        f"/api/v1/chunk-runs/{run}/chunks{query}", headers=auth(credentials, role)
    )
    assert response.status_code == 200, response.text
    return response.json()


def worst(chunk):
    severities = {f["severity"] for f in chunk["findings"]}
    return "blocking" if severities & BLOCKING else "warning" if severities else "clean"


def test_every_chunk_is_counted_once_under_its_worst_finding(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    run = run_id(client, credentials, body)
    summary = client.get(f"/api/v1/chunk-runs/{run}/review-summary", headers=auth(credentials))
    assert summary.status_code == 200
    counts = summary.json()
    everything = listed(client, credentials, run)
    assert counts["chunks"] == everything["total"] > 0
    assert (
        counts["blocking_chunks"] + counts["warning_chunks"] + counts["clean_chunks"]
        == (counts["chunks"])
    )
    by_state = {
        state: [c for c in everything["items"] if worst(c) == state]
        for state in ("blocking", "warning", "clean")
    }
    assert len(by_state["blocking"]) == counts["blocking_chunks"]
    assert len(by_state["warning"]) == counts["warning_chunks"]
    assert len(by_state["clean"]) == counts["clean_chunks"]
    findings = client.get(
        f"/api/v1/chunk-runs/{run}/validation-findings?limit=100", headers=auth(credentials)
    ).json()
    assert counts["findings"] == findings["total"]
    assert counts["blocking_findings"] == sum(f["severity"] in BLOCKING for f in findings["items"])
    assert counts["warning_findings"] == sum(f["severity"] == "WARNING" for f in findings["items"])
    assert counts["dataset_findings"] == sum(f["chunk_id"] is None for f in findings["items"])
    assert counts["questions"] == 0  # a textbook, not a question bank


@pytest.mark.parametrize("status", ["blocking", "warning", "clean", "findings"])
def test_each_status_filter_returns_exactly_its_chunks(chunked, status):  # noqa: F811
    client, _, credentials, body = chunked
    run = run_id(client, credentials, body)
    everything = listed(client, credentials, run)["items"]
    expected = {
        c["id"]
        for c in everything
        if (worst(c) != "clean" if status == "findings" else worst(c) == status)
    }
    result = listed(client, credentials, run, status)
    assert {c["id"] for c in result["items"]} == expected
    assert result["total"] == len(expected)


def test_a_chunk_carries_its_own_findings_in_the_list_and_in_detail(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    run = run_id(client, credentials, body)
    findings = client.get(
        f"/api/v1/chunk-runs/{run}/validation-findings?limit=100", headers=auth(credentials)
    ).json()["items"]
    attached = {}
    for f in findings:
        if f["chunk_id"]:
            attached.setdefault(f["chunk_id"], []).append(f["code"])
    for chunk in listed(client, credentials, run)["items"]:
        assert sorted(f["code"] for f in chunk["findings"]) == sorted(attached.get(chunk["id"], []))
    if attached:
        chunk_id = next(iter(attached))
        detail = client.get(f"/api/v1/chunks/{chunk_id}", headers=auth(credentials)).json()
        assert sorted(f["code"] for f in detail["findings"]) == sorted(attached[chunk_id])


def test_a_reader_can_see_the_review_and_another_tenant_cannot(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    run = run_id(client, credentials, body)
    reader = client.get(
        f"/api/v1/chunk-runs/{run}/review-summary", headers=auth(credentials, READER)
    )
    assert reader.status_code == 200
    for path in (f"/chunk-runs/{run}/review-summary", f"/chunk-runs/{run}/chunks?status=blocking"):
        other = client.get(f"/api/v1{path}", headers=auth(credentials, OTHER_TENANT))
        assert other.status_code == 404


def test_an_unknown_status_is_refused(chunked):  # noqa: F811
    client, _, credentials, body = chunked
    run = run_id(client, credentials, body)
    response = client.get(
        f"/api/v1/chunk-runs/{run}/chunks?status=healthy", headers=auth(credentials)
    )
    assert response.status_code == 422
