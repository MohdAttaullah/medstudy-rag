"""End-to-end M2 smoke against the running containerised stack.

Uploads a synthetic structural fixture through the public nginx entrypoint, waits for the real
Celery parsing worker to reach READY_FOR_CHUNKING, and verifies the persisted structure through
the authorized inspection API. It asserts real behaviour only; nothing is simulated here.

    uv run --env-file .env python scripts/smoke_m2.py
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

settings = Settings()
credential = next(item for item in settings.dev_principals if item.role == "admin")
headers = {"Authorization": "Bearer " + credential.token.get_secret_value()}
payload = FIXTURE.read_bytes() + f"\n%% smoke {uuid4().hex}\n".encode()
metadata = {
    "filename": "m2-smoke.pdf",
    "edition": "Synthetic",
    "document": {
        "title": "M2 parsing smoke " + uuid4().hex[:8],
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
    assert ids["status"] == "QUEUED"
    base = f"/api/v1/documents/{ids['document_id']}/versions/{ids['version_id']}"

    deadline = time.monotonic() + DEADLINE_SECONDS
    summary = {}
    while time.monotonic() < deadline:
        summary = client.get(f"{base}/parse", headers=headers).json()
        if summary["ingestion_status"] in {
            "READY_FOR_CHUNKING",
            "READY_FOR_EMBEDDING",
            "NEEDS_REVIEW",
            "FAILED",
            "QUARANTINED",
        }:
            break
        time.sleep(3)
    # M3 chunks immediately after parsing, so the version may already have advanced past
    # READY_FOR_CHUNKING by the time this poll observes it. Either state proves the parse.
    status = summary.get("ingestion_status")
    assert status in {"READY_FOR_CHUNKING", "READY_FOR_EMBEDDING"}, (
        f"ended in {status}: {summary.get('parse_run')}"
    )

    run = summary["parse_run"]
    assert run["is_active"] and run["status"] == "SUCCEEDED"
    assert run["parser_name"] == "docling" and run["parser_version"]
    assert run["page_count"] == 1 and run["table_count"] == 1
    assert run["element_count"] and run["element_count"] >= 3

    pages = client.get(f"{base}/parse-runs/{run['id']}/pages", headers=headers).json()
    page = pages["items"][0]
    assert page["page_number"] == 1 and page["has_preview"]
    detail = client.get(f"{base}/parse-runs/{run['id']}/pages/{page['id']}", headers=headers).json()
    assert "Tabular Structure Fixture" in detail["extracted_text"]
    preview = client.get(
        f"{base}/parse-runs/{run['id']}/pages/{page['id']}/preview", headers=headers
    )
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("image/")

    tables = client.get(f"{base}/parse-runs/{run['id']}/tables", headers=headers).json()
    table = client.get(
        f"{base}/parse-runs/{run['id']}/tables/{tables['items'][0]['id']}", headers=headers
    ).json()
    assert table["row_count"] == 4 and table["column_count"] == 4 and len(table["cells"]) == 16
    grid = {(cell["row"], cell["column"]): cell["text"] for cell in table["cells"]}
    assert [grid[(0, column)] for column in range(4)] == [
        "Parameter",
        "Group A",
        "Group B",
        "Units",
    ]
    assert [grid[(row, 0)] for row in range(4)] == ["Parameter", "Alpha", "Beta", "Gamma"]
    # The caption sentence must survive as page content. Whether the parser also reports it as a
    # *caption of this table* is a layout-model judgement that varies between CPU environments,
    # so this smoke asserts preservation, not the parser's classification.
    assert "Synthetic parameter table caption" in detail["extracted_text"]

    elements = client.get(
        f"{base}/parse-runs/{run['id']}/elements?element_type=TABLE", headers=headers
    ).json()
    assert elements["total"] == 1
    assert client.get(f"{base}/parse", headers={}).status_code == 401

with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
    assert client.get("/health/live").json()["milestone"] == "M4"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    for series in ("parse_runs_by_status", "parse_runs_by_result", "parse_pages_total"):
        assert series in metrics.text, series

print(
    "PASS: containerised worker parsed a real upload to READY_FOR_CHUNKING; "
    "raw run, page preview, normalized text, structured table and parse metrics verified"
)
