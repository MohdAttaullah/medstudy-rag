import base64
import hashlib
import json
from uuid import uuid4

import httpx
from app.core.config import Settings

settings = Settings()
credential = next(item for item in settings.dev_principals if item.role == "admin")
headers = {"Authorization": "Bearer " + credential.token.get_secret_value()}
with httpx.Client(base_url="http://127.0.0.1:5173", timeout=30) as client:
    assert client.get("/api/health/ready").status_code == 200
    assert client.get("/api/v1/documents").status_code == 401
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    response = client.get("/api/v1/documents", headers=headers)
    assert response.status_code == 200
    doc = response.json()["items"][0]
    version = doc["latest_version"]
    source = client.get(
        f"/api/v1/documents/{doc['id']}/versions/{version['id']}/source", headers=headers
    )
    assert source.status_code == 200
    assert hashlib.sha256(source.content).hexdigest() == version["sha256"]
    assert source.headers["cache-control"] == "no-store"
    metadata = {
        "filename": "invalid.pdf",
        "document": {
            "title": "Proxy validation check",
            "source_type": "OTHER",
            "authority_level": "UNREVIEWED",
            "description": "x" * 4000,
        },
    }
    upload_headers = {
        **headers,
        "Content-Type": "application/pdf",
        "Idempotency-Key": str(uuid4()),
        "X-Upload-Metadata": base64.b64encode(json.dumps(metadata).encode()).decode(),
    }
    invalid = client.post("/api/v1/documents", headers=upload_headers, content=b"invalid PDF")
    assert invalid.status_code == 400, invalid.status_code
    assert invalid.json()["error"]["code"] == "UPLOAD_INVALID_PDF"
    assert client.get(f"/documents/{doc['id']}").status_code == 200
with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
    assert client.get("/health/live").json()["milestone"] == "M1"
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and all(ready.json()["dependencies"].values())
    metrics = client.get("/metrics")
    assert metrics.status_code == 200 and "ingestion_jobs_by_status" in metrics.text
print(
    "PASS: direct API M1/readiness/metrics; nginx auth denial, identity, list, "
    "pinned source SHA-256, bounded metadata upload validation, and SPA deep link"
)
