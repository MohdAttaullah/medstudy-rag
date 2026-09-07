"""Containerized upload-to-Ask smoke.

Drives a real upload to RETRIEVAL_READY, then asks a question through the public endpoint. Whatever
the outcome, the property this exists to prove is that an answer appears only alongside
`verified: true` and a VERIFIED outcome, that every refusal names which kind it was, and that a
rejected draft never reaches the response or the stored turn.
"""

import argparse
import base64
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from app.core.config import Settings

REFUSALS = {"INSUFFICIENT_EVIDENCE", "CONFLICTING_EVIDENCE", "UNVERIFIED", "FAILED"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture", choices=("table", "formula", "figure", "question-bank"), default="table"
    )
    args = parser.parse_args()
    fixture = Path(f"backend/tests/fixtures/parsing/{args.fixture}.pdf")
    settings = Settings()
    actor = next(c for c in settings.dev_principals if c.role == "admin")
    headers = {"Authorization": "Bearer " + actor.token.get_secret_value()}
    meta = {
        "filename": "m9-smoke.pdf",
        "document": {
            "title": "M9 synthetic ask smoke " + uuid4().hex[:8],
            "source_type": "QUESTION_BANK" if args.fixture == "question-bank" else "OTHER",
            "authority_level": "UNREVIEWED",
        },
    }
    payload = fixture.read_bytes() + f"\n%% {uuid4()}\n".encode()
    with httpx.Client(base_url="http://127.0.0.1:5173", headers=headers, timeout=300) as client:
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
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            job = client.get(f"/api/v1/ingestion/jobs/{ids['job_id']}").json()
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

        key = uuid4().hex
        response = client.post(
            "/api/v1/ask",
            json={"question": "Parameter Group Alpha Units", "idempotency_key": key},
        )
        assert response.status_code == 200, response.text
        data = response.json()

        outcome = data["outcome"]
        assert outcome in {"VERIFIED", *REFUSALS}
        # The invariant: an answer exists if and only if the outcome is VERIFIED.
        assert data["verified"] is (outcome == "VERIFIED")
        assert (data["answer"] is not None) is (outcome == "VERIFIED")
        assert data["message"], "every outcome explains itself"
        if outcome == "VERIFIED":
            assert data["citations"] and data["sources"]
            for citation in data["citations"]:
                assert citation["document_id"] and citation["parse_run_id"]
                assert citation["authority_level"] and citation["cited_text"]
                # The citation must resolve to a real page in the tenant's own corpus.
                page = client.get(
                    f"/api/v1/documents/{citation['document_id']}/versions/"
                    f"{citation['document_version_id']}/parse-runs/{citation['parse_run_id']}"
                    "/elements"
                )
                assert page.status_code == 200, page.text
        else:
            assert not data["citations"] and not data["sources"]

        # No internal artefact travels with a public answer.
        for forbidden in ("draft", "sufficiency", "verification", "evidence_set", "reranked"):
            assert forbidden not in data, forbidden
        for forbidden in ('"api_key"', "x-api-key", '"confidence"'):
            assert forbidden not in response.text, forbidden

        # A retry returns the stored turn rather than asking again.
        again = client.post(
            "/api/v1/ask",
            json={"question": "Parameter Group Alpha Units", "idempotency_key": key},
        ).json()
        assert again["turn_id"] == data["turn_id"]

        conversation = client.get(f"/api/v1/conversations/{data['conversation_id']}")
        assert conversation.status_code == 200, conversation.text
        turns = conversation.json()["turns"]
        assert turns and turns[0]["outcome"] == outcome
        assert (turns[0]["answer"] is not None) is (outcome == "VERIFIED")

        assert client.get("/api/v1/conversations/" + str(uuid4())).status_code == 404

        record = {
            "ids": ids,
            "outcome": outcome,
            "verified": data["verified"],
            "citations": len(data["citations"]),
            "sources": len(data["sources"]),
            "reason_codes": data["reason_codes"],
            "stages": data["stages"],
        }
        Path(f".local/m9-smoke-{args.fixture}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        print(
            "PASS: upload reached RETRIEVAL_READY; the Ask endpoint returned "
            f"{outcome} for {args.fixture}; an answer appears only with verified=true."
        )
        print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
