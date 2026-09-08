"""Large-upload behaviour against the real application: acceptance, boundary, clean rejection.

Bodies are generated in memory rather than committed as fixtures. The limit is lowered for these
tests so the boundary can be crossed in a few hundred kilobytes instead of half a gigabyte — the
code path is identical, and asserting the boundary is the point, not moving 512 MiB through a test.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from tests.test_m1_integration import auth, database  # noqa: F401
from tests.test_m1_integration import system as system

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires PostgreSQL"
    ),
]

#: A real, valid PDF padded to an arbitrary size. Bytes after `%%EOF` are ignored by PDF readers,
#: so the document still parses while the transferred body is any length we need. Generating it
#: keeps a 512 MiB fixture out of the repository, and padding a genuine file rather than
#: hand-writing one means these tests exercise the same validation a real upload does.
_BASE_PDF = (ROOT / "backend/tests/fixtures/parsing/basic-text.pdf").read_bytes()


def pdf_of(size: int) -> bytes:
    """A valid PDF of exactly `size` bytes.

    The padding goes *before* the trailer, as a PDF comment, not after `%%EOF`. Validation
    requires `%%EOF` within the last 1024 bytes — an incomplete-download check — so appending
    padding to the end would make every generated file look truncated and would be testing the
    generator rather than the size limit.
    """
    if size < len(_BASE_PDF):
        raise ValueError(f"size must be at least {len(_BASE_PDF)} bytes")
    padding = size - len(_BASE_PDF)
    if padding == 0:
        return _BASE_PDF
    marker = _BASE_PDF.rindex(b"trailer")
    comment = b"%" + b"a" * (padding - 2) + b"\n" if padding >= 2 else b" "
    return _BASE_PDF[:marker] + comment + _BASE_PDF[marker:]


def metadata_header(name: str = "large.pdf") -> str:
    payload = {
        "filename": name,
        "edition": "First",
        "publication_year": 2025,
        "document": {
            "title": f"Large upload {uuid4().hex[:8]}",
            "source_type": "TEXTBOOK",
            "authority_level": "UNREVIEWED",
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def post(client, credentials, body: bytes, *, index: int = 0, name: str = "large.pdf"):
    return client.post(
        "/api/v1/documents",
        headers={
            **auth(credentials, index),
            "Content-Type": "application/pdf",
            "Idempotency-Key": str(uuid4()),
            "X-Upload-Metadata": metadata_header(name),
        },
        content=body,
    )


@pytest.fixture
def bounded(database):  # noqa: F811
    """A stack whose upload limit is small enough to cross in a test."""
    from app.security.auth import DevCredential

    tenant = uuid4()
    credentials = (
        DevCredential(
            token=uuid4().hex,
            user_id=uuid4(),
            tenant_id=tenant,
            display_name="Curator",
            role="admin",
        ),
        DevCredential(
            token=uuid4().hex,
            user_id=uuid4(),
            tenant_id=uuid4(),
            display_name="Other tenant",
            role="admin",
        ),
    )
    settings = Settings(
        _env_file=".env",
        database_url=database,
        dev_principals=credentials,
        ingestion={"max_upload_bytes": 256 * 1024},
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.control, credentials, settings.ingestion.max_upload_bytes


# ------------------------------------------------------------------------------ accept / reject


def test_a_pdf_below_the_limit_is_accepted(bounded):
    client, _, credentials, limit = bounded
    response = post(client, credentials, pdf_of(limit // 2))
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "QUEUED"


def test_a_pdf_exactly_at_the_limit_is_accepted(bounded):
    """The boundary is inclusive: a file of exactly the configured size is within the limit."""
    client, _, credentials, limit = bounded
    response = post(client, credentials, pdf_of(limit))
    assert response.status_code == 201, response.text


def test_a_pdf_one_byte_over_the_limit_is_refused_cleanly(bounded):
    client, _, credentials, limit = bounded
    response = post(client, credentials, pdf_of(limit + 1))
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "UPLOAD_FILE_TOO_LARGE"
    # A clean refusal, not a stack trace or a proxy's HTML page.
    assert "exceeds" in body["error"]["message"].lower()
    for leak in ("Traceback", "nginx", "site-packages"):
        assert leak not in response.text


def test_an_oversized_upload_creates_no_document(bounded):
    client, _, credentials, limit = bounded
    before = client.get("/api/v1/documents", headers=auth(credentials)).json()["total"]
    assert post(client, credentials, pdf_of(limit + 4096)).status_code == 413
    after = client.get("/api/v1/documents", headers=auth(credentials)).json()["total"]
    assert after == before, "a refused upload must leave no partial document behind"


def test_a_lying_content_length_cannot_smuggle_an_oversized_body(bounded):
    """The running byte count is the real enforcement; the header check is only an early exit."""
    client, _, credentials, limit = bounded

    def oversized_stream():
        yield pdf_of(limit + 8192)

    response = client.post(
        "/api/v1/documents",
        headers={
            **auth(credentials),
            "Content-Type": "application/pdf",
            "Idempotency-Key": str(uuid4()),
            "X-Upload-Metadata": metadata_header(),
        },
        content=oversized_stream(),  # chunked: no Content-Length at all
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "UPLOAD_FILE_TOO_LARGE"


# --------------------------------------------------------------------------- preserved contracts


def test_sha256_of_a_large_streamed_upload_is_correct(bounded):
    """Provenance must survive chunked hashing, or every citation traces to the wrong bytes."""
    client, _, credentials, limit = bounded
    body = pdf_of(limit - 1024)
    response = post(client, credentials, body)
    assert response.status_code == 201, response.text
    detail = client.get(
        f"/api/v1/documents/{response.json()['document_id']}", headers=auth(credentials)
    ).json()
    assert detail["latest_version"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert detail["latest_version"]["file_size_bytes"] == len(body)


def test_the_stored_object_matches_the_uploaded_bytes(bounded):
    client, control, credentials, limit = bounded
    body = pdf_of(150 * 1024)
    response = post(client, credentials, body)
    assert response.status_code == 201, response.text
    version_id = response.json()["version_id"]
    with control.sessions() as session:
        from app.models.documents import DocumentVersion

        version = session.get(DocumentVersion, version_id)
        key = version.object_storage_key
    stored = control.storage.stat(key)
    assert stored.size == len(body)
    assert stored.sha256 == hashlib.sha256(body).hexdigest()


def test_a_non_pdf_of_permitted_size_is_still_refused(bounded):
    client, _, credentials, _ = bounded
    response = client.post(
        "/api/v1/documents",
        headers={
            **auth(credentials),
            "Content-Type": "text/plain",
            "Idempotency-Key": str(uuid4()),
            "X-Upload-Metadata": metadata_header("notes.txt"),
        },
        content=b"this is not a pdf",
    )
    assert response.status_code >= 400, response.text
    # Refused for being the wrong type, not for its size: the two checks stay distinct so the
    # user is told the actual reason.
    assert response.json()["error"]["code"] == "UPLOAD_UNSUPPORTED_TYPE"


def test_bytes_that_are_not_a_pdf_are_refused_even_with_a_pdf_content_type(bounded):
    client, _, credentials, _ = bounded
    response = post(client, credentials, b"GIF89a" + b"\x00" * 4096)
    assert response.status_code != 201
    assert "Traceback" not in response.text


def test_a_large_upload_stays_inside_its_tenant(bounded):
    client, _, credentials, limit = bounded
    response = post(client, credentials, pdf_of(limit // 2))
    assert response.status_code == 201
    document = response.json()["document_id"]
    # index 1 is a different tenant.
    assert client.get(
        f"/api/v1/documents/{document}", headers=auth(credentials, 1)
    ).status_code in {
        403,
        404,
    }
    assert client.get("/api/v1/documents", headers=auth(credentials, 1)).json()["total"] == 0


# ------------------------------------------------------------------------------- reported limit


def test_the_effective_limit_is_reported_to_anyone_who_may_upload(bounded):
    client, _, credentials, limit = bounded
    response = client.get("/api/v1/uploads/limits", headers=auth(credentials))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["max_upload_bytes"] == limit
    assert body["allowed_mime_types"] == ["application/pdf"]
    # One PDF per HTTP request: selecting several uploads them one after another.
    assert body["files_per_request"] == 1


def test_the_limit_endpoint_requires_the_upload_capability(system):
    client, _, credentials = system
    assert client.get("/api/v1/uploads/limits").status_code == 401
    # index 1 is the reader, who may not upload.
    assert client.get("/api/v1/uploads/limits", headers=auth(credentials, 1)).status_code == 403
    assert client.get("/api/v1/uploads/limits", headers=auth(credentials)).status_code == 200


def test_the_reported_limit_follows_configuration(database):  # noqa: F811
    from app.security.auth import DevCredential

    credential = DevCredential(
        token=uuid4().hex,
        user_id=uuid4(),
        tenant_id=uuid4(),
        display_name="Curator",
        role="admin",
    )
    settings = Settings(
        _env_file=".env",
        database_url=database,
        dev_principals=(credential,),
        ingestion={"max_upload_bytes": 300 * 1024 * 1024},
    )
    with TestClient(create_app(settings)) as client:
        body = client.get(
            "/api/v1/uploads/limits",
            headers={"Authorization": "Bearer " + credential.token.get_secret_value()},
        ).json()
    assert body["max_upload_bytes"] == 300 * 1024 * 1024
    assert body["max_upload_mib"] == 300
