"""End-to-end M3 smoke against the running containerised stack.

Uploads a synthetic structural fixture through the public entrypoint, waits for the real Celery
worker to parse it and then chunk it to READY_FOR_EMBEDDING, and verifies the persisted chunks,
their provenance back to the exact source element and the question/table handling through the
authorized inspection API. It asserts real behaviour only; nothing is simulated here.

    uv run --env-file .env python scripts/smoke_m3.py
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
# The worker downloads parser weights on its first document of a container lifetime.
DEADLINE_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 900
# M4 carries the job past READY_FOR_EMBEDDING, so this poll may observe the later state.
TERMINAL = {
    "READY_FOR_EMBEDDING",
    "READY_FOR_RETRIEVAL",
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
    "filename": "m3-smoke.pdf",
    "edition": "Synthetic",
    "document": {
        "title": "M3 chunking smoke " + uuid4().hex[:8],
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
    assert job.get("status") in {"READY_FOR_EMBEDDING", "READY_FOR_RETRIEVAL"}, (
        f"ended in {job.get('status')}: "
        f"{job.get('last_error_code')} {job.get('last_error_message')}"
    )
    stages = [event["to_status"] for event in job["events"]]
    chunk_stages = ["CHUNKING", "VALIDATING_CHUNKS", "READY_FOR_EMBEDDING"]
    assert stages[stages.index("CHUNKING") : stages.index("CHUNKING") + 3] == chunk_stages, stages

    runs = client.get(f"{base}/chunk-runs", headers=headers).json()["items"]
    run = next(item for item in runs if item["is_active"])
    assert run["status"] == "SUCCEEDED"
    assert run["validation_result"] in {"PASS", "PASS_WITH_WARNINGS"}
    assert run["chunker_name"] == "medical-structure" and run["input_fingerprint"]
    assert run["tokenizer_name"] == "ncbi/MedCPT-Article-Encoder"
    metrics = run["metrics"]
    assert metrics["chunks"] > 0 and metrics["tables"] >= 1
    # Every eligible source element reached a chunk, and no source text was dropped.
    assert metrics["missing_provenance"] == 0, metrics
    assert metrics["omitted_characters"] == 0, metrics
    assert metrics["split_omitted_characters"] == 0, metrics

    chunks = client.get(f"/api/v1/chunk-runs/{run['id']}/chunks?limit=100", headers=headers).json()[
        "items"
    ]
    kinds = {chunk["chunk_type"] for chunk in chunks}
    assert {"TEXT_PARENT", "TEXT_CHILD"} & kinds, kinds
    assert {"TABLE", "TABLE_PART"} & kinds, kinds

    table = next(c for c in chunks if c["chunk_type"] in {"TABLE", "TABLE_PART"})
    assert "Parameter" in table["normalized_text"]
    assert table["chunk_metadata"]["cells"], "the canonical source cells were not retained"
    detail = client.get(f"/api/v1/chunks/{table['id']}", headers=headers).json()
    assert any(link["table_id"] for link in detail["artifacts"]), detail["artifacts"]

    # Provenance resolves to an exact source region of the active parse run.
    child = next(c for c in chunks if c["chunk_type"] == "TEXT_CHILD")
    sources = client.get(f"/api/v1/chunks/{child['id']}/sources", headers=headers).json()
    assert sources["total"] > 0
    first = sources["items"][0]
    assert first["element"]["page_number"] >= 1
    assert first["end_offset"] > first["start_offset"]
    assert first["element"]["normalized_text"][first["start_offset"] : first["end_offset"]]

    # Retrieval context is declared, never invented content.
    assert child["normalized_text"] in child["retrieval_text"]
    assert child["token_count"] > 0 and child["retrieval_token_count"] >= child["token_count"]

    findings = client.get(
        f"/api/v1/chunk-runs/{run['id']}/validation-findings", headers=headers
    ).json()
    assert all(item["severity"] in {"INFO", "WARNING"} for item in findings["items"]), findings

    version = client.get(f"{base}", headers=headers)
    if version.status_code == 200:
        assert version.json().get("searchable") is False
    assert client.get(f"{base}/chunk-runs").status_code == 401

with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
    assert client.get("/health/live").json()["milestone"] == "M4"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    for series in ("chunk_runs_by_status", "chunk_runs_by_result", "chunks_by_type"):
        assert series in metrics.text, series

print(
    "PASS: containerised worker parsed and chunked a real upload to READY_FOR_EMBEDDING; "
    "chunk provenance, table artifact relation, source coverage and chunk metrics verified. "
    "No embedding, index or retrieval exists."
)
