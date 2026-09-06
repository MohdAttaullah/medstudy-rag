"""Containerized upload-to-retrieval smoke. Produces candidates, never an answer."""

import base64
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from app.core.config import Settings


def main() -> None:
    settings = Settings()
    actor = next(c for c in settings.dev_principals if c.role == "admin")
    headers = {"Authorization": "Bearer " + actor.token.get_secret_value()}
    meta = {
        "filename": "m5-smoke.pdf",
        "document": {
            "title": "M5 synthetic retrieval smoke " + uuid4().hex[:8],
            "source_type": "OTHER",
            "authority_level": "UNREVIEWED",
        },
    }
    payload = (
        Path("backend/tests/fixtures/parsing/table.pdf").read_bytes() + f"\n%% {uuid4()}\n".encode()
    )
    with httpx.Client(base_url="http://127.0.0.1:5173", headers=headers, timeout=90) as client:
        created = client.post(
            "/api/v1/documents",
            headers={
                "Content-Type": "application/pdf",
                "Idempotency-Key": str(uuid4()),
                "X-Upload-Metadata": base64.b64encode(json.dumps(meta).encode()).decode(),
            },
            content=payload,
        )
        assert created.status_code == 201, created.text
        ids = created.json()
        job_path = f"/api/v1/ingestion/jobs/{ids['job_id']}"
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            job = client.get(job_path).json()
            if job["status"] in {
                "RETRIEVAL_READY",
                "FAILED",
                "CANCELLED",
                "NEEDS_REVIEW",
                "QUARANTINED",
            }:
                break
            time.sleep(3)
        assert job["status"] == "RETRIEVAL_READY", (job["status"], job["last_error_code"])
        stages = [e["to_status"] for e in job["events"]]
        assert stages[-3:] == ["SPARSE_INDEXING", "VERIFYING_SPARSE_INDEX", "RETRIEVAL_READY"]
        base = f"/api/v1/documents/{ids['document_id']}/versions/{ids['version_id']}"
        sparse = client.get(base + "/sparse-index").json()
        assert sparse["lanes_aligned"] and sparse["sparse_index"]["is_active"]
        reports = {}
        for mode in ("DENSE_ONLY", "BM25_ONLY", "HYBRID_RRF"):
            response = client.post(
                "/api/v1/retrieval/search",
                json={
                    "query": "Parameter Group Alpha Units",
                    "mode": mode,
                    "filters": {"document_version_ids": [ids["version_id"]]},
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["candidates"] and result["answering_enabled"] is False
            assert "answer" not in result and "confidence" not in result
            for candidate in result["candidates"]:
                assert candidate["provenance"]["document_version_id"] == ids["version_id"]
                sources = client.get(f"/api/v1/chunks/{candidate['chunk_id']}/sources").json()
                assert sources["items"]
            reports[mode] = result["trace"]["durations_ms"]
        Path(".local/m5-smoke.json").write_text(
            json.dumps({"ids": ids, "durations_ms": reports}, indent=2), encoding="utf-8"
        )
        print(
            "PASS: upload through verified dense and lexical indexes; "
            "all three retrieval modes return provenance; no answer."
        )
        print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
