"""One controlled live end-to-end Ask smoke: upload → M5 → M6 → M7 → M8 → M9, with real providers.

Unlike the M7 and M8 live smokes, nothing is stubbed. A synthetic document is generated, uploaded
and ingested through the real worker, then asked about through the public endpoint, so retrieval,
reranking, the sufficiency gate, the real generator, the real verifier, persistence and citation
rendering all run.

The document is generated here rather than reused from the parsing fixtures for a specific reason:
those fixtures are shaped to exercise the parser and their text is deliberately fragmentary, so a
verifier correctly refuses claims drawn from them. This one contains a few short, coherent,
non-sensitive synthetic paragraphs — the kind of prose the pipeline is meant to answer from.

Prints no key, no prompt and no provider error body.
"""

import argparse
import base64
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from app.core.config import Settings
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas

QUESTION = "What does the reference say about the Aurora Index and the units it uses?"

# Invented terminology on purpose: nothing here resembles real clinical guidance, and a model
# cannot answer it from pretrained knowledge — only from the document below.
PARAGRAPHS = [
    "The Aurora Index is a synthetic teaching measure used only in this evaluation corpus.",
    "The Aurora Index is reported in arbitrary units called lumen-equivalents.",
    "A higher Aurora Index reflects a greater proportion of the synthetic sample being counted.",
    "The Aurora Index is not a clinical measurement and has no diagnostic meaning whatsoever.",
]


def build(path: Path) -> None:
    pdf = Canvas(str(path), pagesize=LETTER)
    width, height = LETTER
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(inch, height - inch, "Synthetic Reference: The Aurora Index")
    pdf.setFont("Helvetica", 11)
    y = height - inch - 40
    for paragraph in PARAGRAPHS:
        pdf.drawString(inch, y, paragraph)
        y -= 26
    pdf.showPage()
    pdf.save()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(".local/m9-live-smoke.json"))
    args = parser.parse_args()

    source = Path(".local/m9-live-source.pdf")
    source.parent.mkdir(parents=True, exist_ok=True)
    build(source)

    settings = Settings()
    selection = settings.generator
    assert selection is not None, "No generator is configured; nothing to smoke."
    actor = next(c for c in settings.dev_principals if c.role == "admin")
    headers = {"Authorization": "Bearer " + actor.token.get_secret_value()}
    meta = {
        "filename": "m9-live.pdf",
        "document": {
            "title": "Synthetic Aurora Reference " + uuid4().hex[:8],
            "source_type": "REFERENCE_BOOK",
            "authority_level": "REFERENCE",
        },
    }

    with httpx.Client(base_url="http://127.0.0.1:5173", headers=headers, timeout=300) as client:
        created = client.post(
            "/api/v1/documents",
            headers={
                "Content-Type": "application/pdf",
                "Idempotency-Key": str(uuid4()),
                "X-Upload-Metadata": base64.b64encode(json.dumps(meta).encode()).decode(),
            },
            content=source.read_bytes(),
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

        started = time.monotonic()
        response = client.post(
            "/api/v1/ask",
            json={
                "question": QUESTION,
                "filters": {"document_version_ids": [ids["version_id"]]},
            },
        )
        elapsed = (time.monotonic() - started) * 1000
        assert response.status_code == 200, response.text
        data = response.json()
        outcome = data["outcome"]

        # The invariant holds whatever the outcome.
        assert data["verified"] is (outcome == "VERIFIED")
        assert (data["answer"] is not None) is (outcome == "VERIFIED")
        for forbidden in ("draft", "sufficiency", "verification", "evidence_set"):
            assert forbidden not in data
        for forbidden in ('"api_key"', "x-api-key", '"confidence"'):
            assert forbidden not in response.text

        resolved = []
        if outcome == "VERIFIED":
            assert data["citations"] and data["sources"]
            for citation in data["citations"]:
                assert citation["document_version_id"] == ids["version_id"]
                # Follow the citation the way the UI does, to the authoritative page.
                elements = client.get(
                    f"/api/v1/documents/{citation['document_id']}/versions/"
                    f"{citation['document_version_id']}/parse-runs/{citation['parse_run_id']}"
                    "/elements?limit=200"
                )
                assert elements.status_code == 200, elements.text
                known = {e["id"] for e in elements.json()["items"]}
                for span in citation["spans"]:
                    assert span["element_id"] in known, "a citation named an element that is gone"
                resolved.append(
                    {
                        "title": citation["document_title"],
                        "authority": citation["authority_level"],
                        "pages": citation["pages"],
                        "elements_resolved": len(citation["spans"]),
                        "has_region": any(
                            s["bbox"] and any(v is not None for v in s["bbox"])
                            for s in citation["spans"]
                        ),
                    }
                )

        conversation = client.get(f"/api/v1/conversations/{data['conversation_id']}")
        assert conversation.status_code == 200, conversation.text
        stored = conversation.json()["turns"][0]
        assert stored["outcome"] == outcome
        assert (stored["answer"] is not None) is (outcome == "VERIFIED")

        record = {
            "generator": selection.model_id,
            "verifier": (settings.verifier or selection).model_id,
            "verifier_independent": settings.verifier is not None
            and settings.verifier != selection,
            "outcome": outcome,
            "verified": data["verified"],
            "citations": len(data["citations"]),
            "sources": len(data["sources"]),
            "citations_resolved_to_source": resolved,
            "reason_codes": data["reason_codes"],
            "stages": data["stages"],
            "ask_latency_ms": round(elapsed, 1),
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(record, indent=2), encoding="utf-8")
        verdict = "a verified answer" if outcome == "VERIFIED" else f"a typed refusal ({outcome})"
        print(
            f"PASS: live upload through the whole pipeline produced {verdict}; no unverified text."
        )
        print(json.dumps(record, indent=2))
        if outcome == "VERIFIED":
            print("\n--- verified answer (synthetic document) ---\n" + data["answer"])


if __name__ == "__main__":
    main()
