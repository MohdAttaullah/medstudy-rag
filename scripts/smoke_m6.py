"""Containerized upload-to-retrieval smoke. Produces candidates, never an answer."""

import argparse
import base64
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from app.core.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture", choices=("table", "formula", "figure", "question-bank"), default="table"
    )
    args = parser.parse_args()
    queries = {
        "table": "Parameter Group Alpha Units",
        "formula": "formula equation",
        "figure": "figure illustration",
        "question-bank": "question answer",
    }
    from pypdf import PdfReader

    fixture = Path(f"backend/tests/fixtures/parsing/{args.fixture}.pdf")
    source_text = " ".join(page.extract_text() or "" for page in PdfReader(fixture).pages)
    query = (
        "Parameter Group Alpha Units"
        if args.fixture == "table"
        else " ".join(source_text.split()[:12]) or queries[args.fixture]
    )
    settings = Settings()
    actor = next(c for c in settings.dev_principals if c.role == "admin")
    headers = {"Authorization": "Bearer " + actor.token.get_secret_value()}
    meta = {
        "filename": "m6-smoke.pdf",
        "document": {
            "title": "M6 synthetic retrieval smoke " + uuid4().hex[:8],
            "source_type": "QUESTION_BANK" if args.fixture == "question-bank" else "OTHER",
            "authority_level": "UNREVIEWED",
        },
    }
    payload = fixture.read_bytes() + f"\n%% {uuid4()}\n".encode()
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
                    "query": query,
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
        response = client.post(
            "/api/v1/retrieval/rerank",
            json={
                "query": query,
                "filters": {"document_version_ids": [ids["version_id"]]},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["first_stage"]["candidates"] and result["reranked"]
        evidence = result["evidence_set"]
        assert evidence["evidence_blocks"] and evidence["total_tokens"] <= 4096
        assert result["answering_enabled"] is False and evidence["answering_enabled"] is False
        assert "answer" not in result and "confidence" not in result
        for block in evidence["evidence_blocks"]:
            assert block["document_version_id"] == ids["version_id"]
            assert block["source_element_ids"] and block["source_spans"] and block["pages"]
            # Every contributing chunk must stay reachable, not just the anchor.
            assert block["source_chunk_ids"]
            for chunk_id in block["source_chunk_ids"]:
                linked = client.get(f"/api/v1/chunks/{chunk_id}/sources")
                assert linked.status_code == 200 and linked.json()["items"], linked.text
            for artifact in block["artifacts"]:
                if artifact["kind"] != "FIGURE":
                    checked = client.get(artifact["href"])
                    assert checked.status_code == 200, checked.text
        reports["M6"] = evidence["reranking_trace"]["durations_ms"]
        Path(f".local/m6-evidence-smoke-{args.fixture}.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        Path(f".local/m6-smoke-{args.fixture}.json").write_text(
            json.dumps({"ids": ids, "durations_ms": reports}, indent=2), encoding="utf-8"
        )
        print(
            "PASS: upload through verified dense and lexical indexes; "
            f"all three M5 modes and M6 return provenance for {args.fixture}; no answer."
        )
        print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
