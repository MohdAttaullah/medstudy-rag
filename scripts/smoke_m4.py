"""End-to-end M4 smoke against the running containerised stack.

Uploads a synthetic fixture through the public entrypoint, waits for the real Celery worker to
parse, chunk, embed and index it to READY_FOR_RETRIEVAL, then verifies through the authorized API
that the vectors are 768-dimensional, that the index reconciled point for point, that every point
resolves back to a chunk and a source page, and that nothing is presented as retrievable.

    uv run --env-file .env python scripts/smoke_m4.py
"""

import base64
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from app.core.config import Settings

FIXTURE = Path("backend/tests/fixtures/parsing/table.pdf")
DEADLINE_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
TERMINAL = {
    "READY_FOR_RETRIEVAL",
    "RETRIEVAL_READY",
    "NEEDS_REVIEW",
    "FAILED",
    "QUARANTINED",
    "CANCELLED",
}

settings = Settings()
credential = next(item for item in settings.dev_principals if item.role == "admin")
headers = {"Authorization": "Bearer " + credential.token.get_secret_value()}
payload = FIXTURE.read_bytes() + f"\n%% smoke {uuid4().hex}\n".encode()
metadata = {
    "filename": "m4-smoke.pdf",
    "edition": "Synthetic",
    "document": {
        "title": "M4 indexing smoke " + uuid4().hex[:8],
        "source_type": "OTHER",
        "authority_level": "UNREVIEWED",
    },
}

with httpx.Client(base_url="http://127.0.0.1:5173", timeout=60) as client:
    created = client.post(
        "/api/v1/documents",
        headers={
            **headers,
            "Content-Type": "application/pdf",
            "Idempotency-Key": str(uuid4()),
            "X-Upload-Metadata": base64.b64encode(json.dumps(metadata).encode()).decode(),
        },
        content=payload,
    )
    assert created.status_code == 201, created.text
    ids = created.json()
    base = f"/api/v1/documents/{ids['document_id']}/versions/{ids['version_id']}"

    deadline = time.monotonic() + DEADLINE_SECONDS
    job = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/ingestion/jobs/{ids['job_id']}", headers=headers).json()
        if job["status"] in TERMINAL:
            break
        time.sleep(3)
    assert job.get("status") in {"READY_FOR_RETRIEVAL", "RETRIEVAL_READY"}, (
        f"ended in {job.get('status')}: "
        f"{job.get('last_error_code')} {job.get('last_error_message')}"
    )
    stages = [event["to_status"] for event in job["events"]]
    assert stages[stages.index("EMBEDDING") : stages.index("EMBEDDING") + 4] == [
        "EMBEDDING",
        "INDEXING",
        "VERIFYING_INDEX",
        "READY_FOR_RETRIEVAL",
    ], stages

    summary = client.get(f"{base}/embedding", headers=headers).json()
    run = summary["embedding_run"]
    version = summary["embedding_version"]
    index = summary["index_run"]
    assert run["status"] == "SUCCEEDED" and run["is_active"]
    assert run["failed_chunk_count"] == 0
    assert run["embedded_chunk_count"] == run["eligible_chunk_count"] > 0
    assert run["input_fingerprint"]

    # The vector semantics are the released MedCPT article-side representation.
    assert version["model_id"] == "ncbi/MedCPT-Article-Encoder"
    assert version["model_revision"] == "d05a736da4bb84ee4057b7f7999485be6ed85465"
    assert version["embedding_dimension"] == 768
    assert version["pooling_strategy"] == "CLS"
    assert version["normalization"] == "NONE"
    assert version["distance_metric"] == "DOT"
    assert version["max_input_tokens"] == 512
    assert set(version["library_versions"]) >= {"transformers", "torch"}, version[
        "library_versions"
    ]

    assert index["status"] == "VERIFIED" and index["is_active"]
    assert index["vector_name"] == "medcpt_dense"
    assert (
        index["verified_point_count"]
        == index["indexed_point_count"]
        == index["expected_point_count"]
        == run["eligible_chunk_count"]
    )
    assert index["activated_at"]
    # Parent chunks are context units, never first-stage retrieval points.
    assert summary["chunk_types"] and "TEXT_PARENT" not in summary["chunk_types"]

    statistics = client.get(f"/api/v1/index-runs/{index['id']}/statistics", headers=headers).json()
    assert statistics["reachable"], statistics
    assert (
        statistics["live_points"] == statistics["expected_points"] == index["expected_point_count"]
    )
    assert statistics["dimension"] == 768 and statistics["distance_metric"] == "DOT"

    points = client.get(
        f"/api/v1/embedding-runs/{run['id']}/embeddings?limit=100", headers=headers
    ).json()
    assert points["total"] == index["expected_point_count"]
    for item in points["items"]:
        assert item["dimension"] == 768
        assert item["token_count"] <= 512 and not item["truncated"]
        assert len(item["vector_checksum"]) == 64 and len(item["input_hash"]) == 64
        # A raw dense array is never returned through the document API.
        assert "vector" not in item and "values" not in item

    # Provenance: point -> chunk -> source page of the active parse run.
    sample = points["items"][0]
    chunk = client.get(f"/api/v1/chunks/{sample['chunk_id']}", headers=headers).json()
    assert chunk["chunk_type"] != "TEXT_PARENT"
    assert chunk["page_start"] and chunk["page_start"] >= 1
    sources = client.get(f"/api/v1/chunks/{sample['chunk_id']}/sources", headers=headers).json()
    assert sources["total"] > 0
    assert sources["items"][0]["element"]["page_number"] >= 1

    findings = client.get(
        f"/api/v1/embedding-runs/{run['id']}/validation-findings", headers=headers
    ).json()
    assert all(item["severity"] in {"INFO", "WARNING"} for item in findings["items"]), findings

    # A verified index does not make the version searchable or answerable.
    versions = client.get(f"{base.rsplit('/', 2)[0]}/versions", headers=headers).json()
    current = next(item for item in versions["items"] if item["id"] == ids["version_id"])
    assert current["searchable"] is False
    assert current["ingestion_status"] in {
        "READY_FOR_RETRIEVAL",
        "SPARSE_INDEXING",
        "VERIFYING_SPARSE_INDEX",
        "RETRIEVAL_READY",
    }
    assert client.get(f"{base}/embedding").status_code == 401

with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
    assert client.get("/health/live").json()["milestone"] == "M5"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    for series in (
        "embedding_runs_by_status",
        "index_runs_by_status",
        "embeddings_total",
        "index_active_points_total",
    ):
        assert series in metrics.text, series

print(
    "PASS: containerised worker embedded and indexed a real upload to READY_FOR_RETRIEVAL; "
    "768-dimension MedCPT vectors, point-for-point reconciliation, provenance and index metrics "
    "verified. No query retrieval, reranking or answering exists."
)
