"""Post-M12 large-document ingestion: limits, bounded memory, and proxy agreement.

No large fixture is committed. Where a big body is needed it is generated in memory or streamed
from a generator, so the repository stays small and the tests stay fast.
"""

import io
import re
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.ingestion_config import IngestionConfig
from app.core.parsing_config import ParsingConfig
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024


# ------------------------------------------------------------------------------ configured limit


def test_the_default_limit_supports_a_real_medical_textbook():
    """128 MiB rejected a real 153 MiB book. The default now covers the intended corpus."""
    config = IngestionConfig()
    assert config.max_upload_bytes == 512 * MIB
    assert config.max_upload_bytes > 153 * MIB, "must accept the 153 MiB acceptance-test book"


def test_the_limit_is_bounded_and_cannot_be_made_unlimited():
    """An explicit ceiling stays. Accepting an arbitrarily large PDF would move the failure from
    a clean rejection at the door to a worker dying halfway through parsing."""
    assert IngestionConfig.model_fields["max_upload_bytes"].metadata
    with pytest.raises(ValidationError):
        IngestionConfig(max_upload_bytes=4 * 1024 * MIB)
    with pytest.raises(ValidationError):
        IngestionConfig(max_upload_bytes=0)
    with pytest.raises(ValidationError):
        IngestionConfig(max_upload_bytes=-1)


def test_the_limit_is_configurable_through_the_existing_settings_mechanism():
    """No new configuration mechanism: the same MEDRAG_ prefix and nested delimiter as everything
    else, and the same M10 registry entry that already reported it."""
    settings = Settings(ingestion={"max_upload_bytes": 256 * MIB})
    assert settings.ingestion.max_upload_bytes == 256 * MIB


def test_the_upload_timeout_allows_a_large_slow_transfer():
    config = IngestionConfig()
    assert config.upload_timeout_seconds >= 1800


def test_the_m10_registry_still_reports_the_limit_read_only():
    """It stays SYSTEM scope and read-only: the reverse proxy cannot read a tenant's runtime
    configuration, so a per-tenant upload limit would silently disagree with the proxy."""
    from app.configuration.registry import REGISTRY, value

    entry = REGISTRY["ingestion.max_upload_bytes"]
    assert entry.scope == "SYSTEM" and entry.editable is False
    assert entry.lifecycle == "RESTART_REQUIRED"
    assert value(Settings(), "ingestion.max_upload_bytes") == 512 * MIB


# --------------------------------------------------------------------- proxy / application accord


def nginx_template() -> str:
    return (ROOT / "infrastructure/docker/nginx.conf.template").read_text(encoding="utf-8")


def test_the_proxy_limit_is_templated_not_hardcoded():
    """Two hardcoded numbers is how the proxy rejected a document the application accepted."""
    template = nginx_template()
    assert "client_max_body_size ${MEDRAG_MAX_UPLOAD_MB}m;" in template
    assert not re.search(r"client_max_body_size\s+\d+m;", template)


def test_the_proxy_never_allows_an_unlimited_body():
    assert "client_max_body_size 0" not in nginx_template()


def test_the_proxy_streams_rather_than_buffering_a_large_upload():
    """Buffering 512 MiB to proxy disk first would double the write and delay the application's
    own size check until the whole body had landed."""
    assert "proxy_request_buffering off;" in nginx_template()


def test_the_shipped_proxy_default_is_at_least_the_application_limit():
    dockerfile = (ROOT / "infrastructure/docker/frontend.Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"MEDRAG_MAX_UPLOAD_MB=(\d+)", dockerfile)
    assert match, "the image must ship a default proxy limit"
    assert int(match.group(1)) * MIB >= IngestionConfig().max_upload_bytes


def test_compose_drives_both_limits_from_the_same_intent():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "MEDRAG_INGESTION__MAX_UPLOAD_BYTES" in compose
    assert "MEDRAG_MAX_UPLOAD_MB" in compose


def test_preflight_fails_when_the_proxy_limit_is_below_the_application_limit(monkeypatch):
    """The exact original misconfiguration: application 512 MiB, proxy 130 MiB."""
    import scripts.production_preflight as preflight  # type: ignore[import-not-found]

    monkeypatch.setenv("MEDRAG_MAX_UPLOAD_MB", "130")
    report = preflight.Report()
    preflight.check_upload_limits(report, Settings())
    failures = [c for c in report.checks if c["status"] == preflight.FAIL]
    assert failures and "proxy would reject" in failures[0]["detail"]


def test_preflight_passes_when_the_proxy_allows_at_least_the_application_limit(monkeypatch):
    import scripts.production_preflight as preflight  # type: ignore[import-not-found]

    monkeypatch.setenv("MEDRAG_MAX_UPLOAD_MB", "520")
    monkeypatch.setenv("MEDRAG_UPLOAD_TIMEOUT_SECONDS", "1800")
    report = preflight.Report()
    preflight.check_upload_limits(report, Settings())
    assert not [c for c in report.checks if c["status"] == preflight.FAIL]


# ------------------------------------------------------------------------------- bounded memory


def test_the_upload_endpoint_never_reads_the_whole_body_into_memory():
    """The property that makes a 512 MiB limit safe at all.

    Asserted against the source because there is no way to observe "did not allocate 512 MiB"
    from outside. The endpoint must iterate `request.stream()` and must not call the whole-body
    readers.
    """
    source = (ROOT / "backend/app/api/documents.py").read_text(encoding="utf-8")
    assert "async for incoming in request.stream():" in source
    for forbidden in ("await request.body()", "await file.read()", "request.form()"):
        assert forbidden not in source, f"whole-body read {forbidden!r} in the upload path"


def test_hashing_and_writing_happen_in_bounded_blocks():
    source = (ROOT / "backend/app/api/documents.py").read_text(encoding="utf-8")
    assert "config.stream_chunk_bytes" in source
    assert "digest.update(block)" in source
    assert IngestionConfig().stream_chunk_bytes <= MIB


def test_object_storage_uploads_in_bounded_multipart_chunks():
    """boto3 would otherwise buffer the whole object; the transfer config caps it at 8 MiB."""
    source = (ROOT / "backend/app/services/storage.py").read_text(encoding="utf-8")
    assert "upload_fileobj" in source
    assert "multipart_chunksize" in source
    assert "put_object" not in source.split("def put_bytes")[0], (
        "the original-PDF path must stream, not send a whole body"
    )


def test_streamed_hashing_matches_a_whole_file_hash():
    """Chunked SHA-256 must equal the hash of the same bytes read at once, or provenance breaks."""
    import hashlib

    payload = b"%PDF-1.7\n" + bytes(range(256)) * 4096
    expected = hashlib.sha256(payload).hexdigest()
    digest = hashlib.sha256()
    stream = io.BytesIO(payload)
    while block := stream.read(IngestionConfig().stream_chunk_bytes):
        digest.update(block)
    assert digest.hexdigest() == expected


# ------------------------------------------------------------------------------- worker bounds


def test_the_worker_task_is_time_bounded_so_one_book_cannot_hold_the_slot():
    parsing = ParsingConfig()
    assert parsing.timeout_seconds <= parsing.max_document_timeout_seconds
    assert (
        parsing.max_document_timeout_seconds + parsing.timeout_seconds
        < parsing.task_soft_timeout_seconds
    ), "the soft limit must clear the document budget plus the window still in flight"
    assert parsing.task_soft_timeout_seconds < parsing.task_timeout_seconds


@pytest.mark.parametrize(
    "override",
    [
        # A document given less time than one conversion call is allowed to take.
        {"max_document_timeout_seconds": 600},
        # Soft limit inside the document budget: the worker would fire before the parser can.
        {"task_soft_timeout_seconds": 15000},
        # Hard limit below the soft limit: no grace period to record a failure.
        {"task_timeout_seconds": 15000},
        # A lease shorter than one conversion call, which nothing can renew from inside.
        {"lease_seconds": 600},
    ],
)
def test_out_of_order_timeouts_are_refused(override):
    """Out of order, the outer limit fires first and the parser's diagnosable error is lost."""
    with pytest.raises(ValidationError):
        ParsingConfig(**override)


def test_celery_applies_the_configured_task_limits():
    source = (ROOT / "workers/celery_app.py").read_text(encoding="utf-8")
    assert "task_time_limit=config.parsing.task_timeout_seconds" in source
    assert "task_soft_time_limit=config.parsing.task_soft_timeout_seconds" in source


def test_parsing_stays_bounded_by_pages_and_concurrency():
    parsing = ParsingConfig()
    assert parsing.max_pages <= 20000
    assert parsing.max_concurrency == 1, "models are memory-heavy; one parse per worker"


# ------------------------------------------------------------- validation budget scales with size


def test_validation_timeout_scales_with_file_size():
    """A flat 20 s budget timed out on a real 153 MiB textbook during acceptance testing.

    Scaling is what lets one setting serve a 1 MiB leaflet and a 512 MiB book: a small hostile
    PDF still fails fast instead of inheriting the allowance a large legitimate one needs.
    """
    config = IngestionConfig()
    small = config.validation_timeout_for(1 * MIB)
    book = config.validation_timeout_for(153 * MIB)
    largest = config.validation_timeout_for(config.max_upload_bytes)
    assert small < book < largest
    assert book > 60, "a 153 MiB textbook needs more than the old flat 20 s"
    assert small < 60, "a small PDF must still fail fast"


def test_the_validation_budget_has_a_hard_ceiling():
    """Validation runs in a subprocess; without a ceiling a pathological file could hold the
    request path for as long as it liked."""
    config = IngestionConfig()
    assert config.validation_timeout_for(10 * 1024 * MIB) == config.validation_timeout_max_seconds
    assert config.validation_timeout_max_seconds <= 3600


def test_validation_timeout_is_never_zero_or_negative():
    config = IngestionConfig(validation_seconds_per_mib=0)
    assert config.validation_timeout_for(0) >= 1
    assert config.validation_timeout_for(512 * MIB) >= 1
