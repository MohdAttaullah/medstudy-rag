import io
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.errors import DomainError
from app.core.ingestion_config import IngestionConfig
from app.ingestion.state import require_transition
from app.ingestion.validation.files import checksum, sanitize_filename, validate_mime, validate_pdf
from app.models.enums import Status
from app.schemas.documents import DocumentMetadata
from app.security.auth import DevAuthProvider, DevCredential, Principal
from app.services.storage import object_key
from pydantic import ValidationError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "before,after",
    [
        ("UPLOADED", "VALIDATING"),
        ("VALIDATING", "QUEUED"),
        ("QUEUED", "FAILED"),
        ("QUEUED", "CANCELLED"),
        ("VALIDATING", "QUARANTINED"),
    ],
)
def test_allowed_transition(before, after):
    require_transition(Status(before), Status(after))


@pytest.mark.parametrize(
    "before,after",
    [
        ("UPLOADED", "READY"),
        ("QUEUED", "READY"),
        ("FAILED", "PARSING"),
        ("CANCELLED", "VALIDATING"),
        ("QUARANTINED", "QUEUED"),
        # QUEUED -> PARSING became executable in M2; QUEUED -> READY_FOR_CHUNKING never is.
        ("QUEUED", "READY_FOR_CHUNKING"),
    ],
)
def test_illegal_transition(before, after):
    with pytest.raises(DomainError, match="INGESTION_INVALID_TRANSITION"):
        require_transition(Status(before), Status(after))


def test_retry_is_only_failed_to_validating():
    require_transition(Status.FAILED, Status.VALIDATING, retry=True)
    with pytest.raises(DomainError):
        require_transition(Status.UPLOADED, Status.VALIDATING, retry=True)


@pytest.mark.parametrize("name", ["../x.pdf", "a\\x.pdf", "C:x.pdf", "x.exe", "x\n.pdf"])
def test_unsafe_filename_rejected(name):
    with pytest.raises(DomainError):
        sanitize_filename(name)


def test_filename_and_object_identity():
    assert sanitize_filename("A textbook (2).PDF") == "A_textbook_2.pdf"
    document, version = uuid4(), uuid4()
    assert object_key(document, version) == f"documents/{document}/{version}/original/source.pdf"
    assert "textbook" not in object_key(document, version)


def test_streamed_checksum_does_not_read_all_at_once():
    class Bounded(io.BytesIO):
        def read(self, n=-1):
            assert 0 < n <= 3
            return super().read(n)

    assert checksum(Bounded(b"abc"), 3) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize("mime", [None, "text/plain", "application/octet-stream", "image/png"])
def test_mime_rejected(mime):
    with pytest.raises(DomainError):
        validate_mime(mime, IngestionConfig())


def test_pdf_structure_and_snapshot():
    config = IngestionConfig()
    validate_pdf(FIXTURES / "valid.pdf", config)
    snapshot = config.model_dump(mode="json")
    assert snapshot["duplicate_policy"] == "reject-within-tenant"
    assert IngestionConfig(max_upload_bytes=2048).max_upload_bytes != snapshot["max_upload_bytes"]
    for name in ["malformed.pdf", "wrong-content.pdf"]:
        with pytest.raises(DomainError):
            validate_pdf(FIXTURES / name, config)


def test_metadata_does_not_promote_assessment_keys():
    with pytest.raises(ValidationError):
        DocumentMetadata(title="Key", source_type="ANSWER_KEY", authority_level="HIGH")
    with pytest.raises(ValidationError):
        DocumentMetadata(title=" ", source_type="TEXTBOOK")
    with pytest.raises(ValidationError):
        DocumentMetadata(title="Book", source_type="TEXTBOOK", user_id=str(uuid4()))


def test_principal_is_derived_from_verified_credential():
    credential = DevCredential(
        token="a" * 40, user_id=uuid4(), tenant_id=uuid4(), display_name="Reader", role="reader"
    )
    auth = DevAuthProvider((credential,))
    actor = auth.authenticate("Bearer " + "a" * 40)
    assert actor.user_id == credential.user_id
    actor.require("document:read")
    with pytest.raises(DomainError, match="FORBIDDEN"):
        actor.require("document:upload")
    with pytest.raises(DomainError, match="UNAUTHORIZED"):
        auth.authenticate("Bearer user-supplied-id")
    assert "a" * 40 not in repr(credential)


def test_denied_role():
    with pytest.raises(DomainError):
        Principal(uuid4(), uuid4(), "Reader", "reader").require("document:manage")
