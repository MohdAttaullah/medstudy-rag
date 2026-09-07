"""Containerized upload-to-verified-answer smoke.

Drives a real upload to RETRIEVAL_READY and then through the M8 endpoint. Whatever the outcome, the
property this smoke exists for is that `verified` is true only when a verified answer is present and
every material claim in it was checked — and that no path returns an unverified answer.
"""

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
    fixture = Path(f"backend/tests/fixtures/parsing/{args.fixture}.pdf")
    query = "Parameter Group Alpha Units"
    settings = Settings()
    actor = next(c for c in settings.dev_principals if c.role == "admin")
    headers = {"Authorization": "Bearer " + actor.token.get_secret_value()}
    meta = {
        "filename": "m8-smoke.pdf",
        "document": {
            "title": "M8 synthetic verification smoke " + uuid4().hex[:8],
            "source_type": "QUESTION_BANK" if args.fixture == "question-bank" else "OTHER",
            "authority_level": "UNREVIEWED",
        },
    }
    payload = fixture.read_bytes() + f"\n%% {uuid4()}\n".encode()
    with httpx.Client(base_url="http://127.0.0.1:5173", headers=headers, timeout=180) as client:
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
        # RETRIEVAL_READY still is not answer readiness, and READY stays unreachable.
        assert "READY" not in [e["to_status"] for e in job["events"]]

        body = {
            "query": query,
            "filters": {"document_version_ids": [ids["version_id"]]},
        }
        response = client.post("/api/v1/retrieval/answer", headers=headers, json=body)
        outcome: dict[str, object]
        if response.status_code == 200:
            data = response.json()
            assert data["answering_enabled"] is False and data["verified"] is False
            decision = data["sufficiency"]
            assert decision["status"] in {"SUFFICIENT", "INSUFFICIENT", "CONFLICTING"}
            assert decision["policy_fingerprint"] and decision["evaluated_signals"]
            supplied = {b["evidence_id"] for b in data["evidence_set"]["evidence_blocks"]}
            report = data["verification"]
            if data["draft"] is None:
                assert data["abstention"], "a refused decision must say why"
                assert decision["status"] != "SUFFICIENT"
                assert report is None and data["verified"] is False
            else:
                assert decision["status"] == "SUFFICIENT"
                assert (
                    data["draft"]["verification_status"] == "UNVERIFIED_AWAITING_CLAIM_VERIFICATION"
                )
                for claim in data["draft"]["claims"]:
                    assert set(claim["evidence_ids"]) <= supplied
                assert report, "a draft that was produced must have been verified"
                assert report["outcome"] in {"PASS", "ABSTAIN"}
                assert report["repair_count"] <= 1, "a repair happens at most once"
            # verified is true only behind a released verified answer, never on its own.
            answer = data["verified_answer"]
            assert data["verified"] is (answer is not None)
            if answer is None:
                assert data["verification_abstention"] or data["abstention"] or report is None
            else:
                assert answer["verified"] is True
                assert answer["verification_status"] == "VERIFIED"
                assert report["outcome"] == "PASS"
                assert set(answer["cited_evidence_ids"]) <= supplied
                for claim in answer["claims"]:
                    assert claim["verdict"] == "SUPPORTED"
            outcome = {
                "http": 200,
                "sufficiency": decision["status"],
                "verification_outcome": report["outcome"] if report else None,
                "verified": data["verified"],
                "material_claims": report["material_claims"] if report else 0,
                "supported_claims": report["supported_claims"] if report else 0,
                "repair_count": report["repair_count"] if report else 0,
                "reason_codes": (
                    report["failed_reason_codes"] if report else decision["reason_codes"]
                ),
                "durations_ms": data["durations_ms"],
            }
        else:
            code = response.json()["error"]["code"]
            assert code.startswith(("GENERATION_", "VERIFIER_")), response.text
            outcome = {"http": response.status_code, "declared_failure": code}

        # Whatever happened, no shape of this response is a delivered medical answer.
        text = response.text
        assert '"answering_enabled": true' not in text
        # `verified: true` is legitimate here, but only inside a released verified answer; a
        # confidence figure never is, because M8 establishes no calibrated certainty.
        for forbidden in ('"medical_confidence"', '"confidence"'):
            assert forbidden not in text, forbidden

        Path(f".local/m8-smoke-{args.fixture}.json").write_text(
            json.dumps({"ids": ids, "outcome": outcome}, indent=2), encoding="utf-8"
        )
        print(
            "PASS: upload reached RETRIEVAL_READY; the gate decided, and nothing was released as "
            f"verified without every material claim being checked for {args.fixture}."
        )
        print(json.dumps(outcome, indent=2))


if __name__ == "__main__":
    main()
