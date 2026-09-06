"""M2 integration tests against the real stack.

These run the actual pipeline: a real upload through the API, a real Docling parse, real MinIO
artifacts, real PostgreSQL rows and the real tenant-authorized inspection API. Docling model
weights are cached after the first run; the module-scoped parse fixtures keep conversions to
the minimum needed to cover the milestone.
"""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.parsing_config import ParseThresholds, ParsingConfig
from app.ingestion.parser.errors import (
    PARSER_SOURCE_CORRUPT,
    PARSER_SOURCE_MISSING,
    RAW_ARTIFACT_STORAGE_FAILED,
    ParserError,
)
from app.models.documents import DocumentVersion, IngestionJob, IngestionStageEvent, OutboxMessage
from app.models.enums import ElementType, OcrMode, ParseResult, ParseRunStatus, Severity, Status
from app.models.parsing import (
    DocumentElement,
    DocumentPage,
    FigureArtifact,
    FormulaArtifact,
    ParseRun,
    ParseValidationFinding,
    TableArtifact,
)
from app.observability.parsing import ParseMetrics
from app.services.parsing import ParseService, reap_expired_leases
from app.services.queue import dispatch, receive
from prometheus_client import CollectorRegistry
from sqlalchemy import select
from tests.test_m1_integration import auth, database, upload  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1",
        reason="Requires PostgreSQL, MinIO and the installed parser",
    ),
]
FIXTURES = Path(__file__).parent / "fixtures/parsing"


class Recorded:
    """A parser stand-in that replays or fails deterministically, without loading Docling."""

    name = "docling"
    provider = "docling-project"

    def __init__(self, result, version="test-parser"):
        self.result, self.version, self.calls = result, version, 0

    def parse(self, source, config):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def parser():
    from app.ingestion.parser.docling_adapter import DoclingDocumentParser

    return DoclingDocumentParser()


def make_service(control, config=None, parser_impl=None, worker="celery-parser"):
    return ParseService(
        control.sessions,
        control.storage,
        parser_impl if parser_impl is not None else parser(),
        config or ParsingConfig(),
        ParseMetrics(CollectorRegistry()),
        worker_identity=worker,
    )


def unique(fixture: str) -> bytes:
    """Fixture bytes with a trailing PDF comment.

    Tenant-wide SHA-256 uniqueness is an M1 rule these tests must respect, so each upload of the
    same fixture gets a distinct checksum without altering a single byte of parseable content.
    """
    marker = f"\n%% unique {uuid4().hex}\n".encode()
    return (FIXTURES / fixture).read_bytes() + marker


def queue_job(client, control, credentials, fixture: str):
    """Upload a fixture through the real API and advance it to QUEUED through the real outbox."""
    response = upload(client, credentials, file=fixture, content=unique(fixture))
    assert response.status_code == 201, response.text
    body = response.json()
    with control.sessions.begin() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.job_id == uuid_of(body["job_id"]))
        )
        message_id = message.id
    claimed = receive(control.sessions, control.storage, message_id)
    assert claimed is not None
    return body, message_id


def uuid_of(value):
    from uuid import UUID

    return UUID(value)


def run_of(control, version_id):
    with control.sessions() as session:
        return session.scalar(
            select(ParseRun).where(ParseRun.document_version_id == uuid_of(version_id))
        )


def page_text_of(control, run_id, page_number):
    with control.sessions() as session:
        return session.scalar(
            select(DocumentPage.extracted_text).where(
                DocumentPage.parse_run_id == run_id, DocumentPage.page_number == page_number
            )
        )


# --------------------------------------------------------------------- happy path (real parse)


@pytest.fixture(scope="module")
def parsed_table(system_module):
    """One real Docling parse of the table fixture, reused by the assertions below."""
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "table.pdf")
    outcome = make_service(control).run(uuid_of(body["job_id"]))
    return client, control, credentials, body, outcome


@pytest.fixture(scope="module")
def system_module(database):  # noqa: F811
    """Module-scoped stack so an expensive real parse is performed once, not per assertion."""
    from app.core.config import Settings
    from app.main import create_app
    from app.security.auth import DevCredential
    from fastapi.testclient import TestClient

    tenant = uuid4()
    credentials = (
        DevCredential(
            token=uuid4().hex, user_id=uuid4(), tenant_id=tenant, display_name="A", role="admin"
        ),
        DevCredential(
            token=uuid4().hex, user_id=uuid4(), tenant_id=tenant, display_name="R", role="reader"
        ),
        DevCredential(
            token=uuid4().hex, user_id=uuid4(), tenant_id=uuid4(), display_name="O", role="admin"
        ),
    )
    settings = Settings(_env_file=".env", database_url=database, dev_principals=credentials)
    app = create_app(settings)
    control = app.state.control
    with TestClient(app) as client:
        yield client, control, credentials
    # Each milestone downgrade refuses to run while its own completed states still exist, so this
    # teardown performs the explicit operator action those messages prescribe before the schema is
    # reversed. Nothing is rewritten silently; the jobs are cancelled through the real guard.
    from app.ingestion.state import transition

    completed = (
        Status.READY_FOR_CHUNKING,
        Status.READY_FOR_EMBEDDING,
        Status.READY_FOR_RETRIEVAL,
    )
    with control.sessions.begin() as session:
        for job in session.scalars(select(IngestionJob).where(IngestionJob.status.in_(completed))):
            version = session.get(DocumentVersion, job.document_version_id)
            transition(session, job, version, Status.CANCELLED, None, service="test-teardown")
    with control.sessions() as session:
        keys = [
            key
            for key in session.scalars(
                select(DocumentVersion.object_storage_key).where(
                    DocumentVersion.tenant_id.in_([tenant, credentials[2].tenant_id])
                )
            )
        ]
        keys += [
            run.raw_artifact_key
            for run in session.scalars(select(ParseRun))
            if run.raw_artifact_key
        ]
        keys += [
            page.preview_key for page in session.scalars(select(DocumentPage)) if page.preview_key
        ]
        keys += [
            figure.image_key
            for figure in session.scalars(select(FigureArtifact))
            if figure.image_key
        ]
    for key in keys:
        control.storage.delete(key)


def test_real_parse_reaches_ready_for_chunking(parsed_table):
    _, control, _, body, outcome = parsed_table
    assert outcome.status is Status.READY_FOR_CHUNKING
    assert outcome.result in {ParseResult.PASS, ParseResult.PASS_WITH_WARNINGS}
    with control.sessions() as session:
        version = session.get(DocumentVersion, uuid_of(body["version_id"]))
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
        assert version.ingestion_status is Status.READY_FOR_CHUNKING
        assert version.searchable is False  # M2 never makes a version retrievable
        assert version.page_count == 1
        assert job.status is Status.READY_FOR_CHUNKING
        history = [
            event.to_status
            for event in session.scalars(
                select(IngestionStageEvent)
                .where(IngestionStageEvent.ingestion_job_id == job.id)
                .order_by(IngestionStageEvent.sequence)
            )
        ]
    assert history == [
        "UPLOADED",
        "VALIDATING",
        "QUEUED",
        "PARSING",
        "NORMALIZING",
        "ENRICHING",
        "READY_FOR_CHUNKING",
    ]


def test_parse_run_records_reproducible_provenance(parsed_table):
    _, control, _, body, _ = parsed_table
    run = run_of(control, body["version_id"])
    assert run.status is ParseRunStatus.SUCCEEDED and run.is_active
    assert run.parser_name == "docling" and run.parser_version
    assert run.configuration_version == ParsingConfig().version
    assert run.configuration_fingerprint == ParsingConfig().fingerprint
    assert run.config_snapshot["ocr_mode"] == OcrMode.AUTO.value
    assert run.source_checksum and run.source_object_version_id
    assert run.attempt == 1 and run.page_count == 1 and run.source_page_count == 1


def test_raw_docling_artifact_is_persisted_in_object_storage(parsed_table):
    _, control, _, body, _ = parsed_table
    run = run_of(control, body["version_id"])
    assert run.raw_artifact_key.endswith(f"parsing/{run.id}/docling.json")
    assert f"documents/{body['document_id']}/{body['version_id']}/parsing/" in run.raw_artifact_key
    payload = json.loads(control.storage.read(run.raw_artifact_key, run.raw_artifact_version_id))
    assert payload["parser"]["name"] == "docling"
    assert payload["source_page_count"] == 1
    assert payload["document"]["pages"]  # complete structured parser output, not flattened text
    assert run.raw_artifact_bytes == len(
        control.storage.read(run.raw_artifact_key, run.raw_artifact_version_id)
    )


def test_pages_elements_and_table_structure_persist(parsed_table):
    _, control, _, body, _ = parsed_table
    run = run_of(control, body["version_id"])
    with control.sessions() as session:
        pages = list(
            session.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == run.id))
        )
        elements = list(
            session.scalars(
                select(DocumentElement)
                .where(DocumentElement.parse_run_id == run.id)
                .order_by(DocumentElement.reading_order)
            )
        )
        table = session.scalar(select(TableArtifact).where(TableArtifact.parse_run_id == run.id))
    assert [page.page_number for page in pages] == [1]  # 1-based, never 0-based
    assert pages[0].width > 0 and pages[0].height > 0
    assert pages[0].preview_key  # page preview rendered and stored
    assert [element.reading_order for element in elements] == list(range(len(elements)))
    types = {element.element_type for element in elements}
    assert ElementType.HEADING in types and ElementType.TABLE in types
    assert ElementType.CAPTION in types
    located = [element for element in elements if element.bbox_x1 is not None]
    assert located and all(
        element.bbox_x2 > element.bbox_x1 and element.bbox_y2 > element.bbox_y1
        for element in located
    )
    assert all(element.bbox_origin.value == "TOPLEFT" for element in located)

    assert table.row_count == 4 and table.column_count == 4
    assert len(table.cells) == 16
    grid = {(cell["row"], cell["column"]): cell["text"] for cell in table.cells}
    assert [grid[(0, column)] for column in range(4)] == [
        "Parameter",
        "Group A",
        "Group B",
        "Units",
    ]
    assert [grid[(row, 0)] for row in range(4)] == ["Parameter", "Alpha", "Beta", "Gamma"]
    assert grid[(3, 3)] == "mL"
    # Header-row detection is a parser judgement; the count must nonetheless agree with the
    # cells actually stored, so the two can never drift apart in our own persistence.
    assert table.header_row_count == len(
        {cell["row"] for cell in table.cells if cell["column_header"]}
    )
    assert (table.caption_element_id is None) == (table.caption_text is None)
    assert "Synthetic parameter table caption" in page_text_of(control, run.id, 1)
    assert not table.malformed


def test_inspection_api_is_tenant_scoped_and_paginated(parsed_table):
    client, control, credentials, body, _ = parsed_table
    run = run_of(control, body["version_id"])
    base = f"/api/v1/documents/{body['document_id']}/versions/{body['version_id']}"

    summary = client.get(f"{base}/parse", headers=auth(credentials)).json()
    assert summary["ingestion_status"] == "READY_FOR_CHUNKING"
    assert summary["parse_run"]["is_active"] and summary["parse_runs"] == 1
    assert summary["parse_run"]["table_count"] == 1

    runs = client.get(f"{base}/parse-runs", headers=auth(credentials)).json()
    assert runs["total"] == 1 and runs["limit"] == 20

    pages = client.get(f"{base}/parse-runs/{run.id}/pages", headers=auth(credentials)).json()
    assert pages["items"][0]["page_number"] == 1 and pages["items"][0]["has_preview"]
    page_id = pages["items"][0]["id"]

    detail = client.get(
        f"{base}/parse-runs/{run.id}/pages/{page_id}", headers=auth(credentials)
    ).json()
    assert "Tabular Structure Fixture" in detail["extracted_text"]

    preview = client.get(
        f"{base}/parse-runs/{run.id}/pages/{page_id}/preview", headers=auth(credentials)
    )
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("image/")
    assert preview.headers["cache-control"] == "no-store"

    elements = client.get(
        f"{base}/parse-runs/{run.id}/elements?element_type=TABLE", headers=auth(credentials)
    ).json()
    assert elements["total"] == 1 and elements["items"][0]["bbox"]["origin"] == "TOPLEFT"

    tables = client.get(f"{base}/parse-runs/{run.id}/tables", headers=auth(credentials)).json()
    table_id = tables["items"][0]["id"]
    table = client.get(
        f"{base}/parse-runs/{run.id}/tables/{table_id}", headers=auth(credentials)
    ).json()
    assert len(table["cells"]) == 16 and table["markdown"]

    # A reader may inspect; only a curator/admin may fetch the raw parser artifact.
    assert client.get(f"{base}/parse", headers=auth(credentials, 1)).status_code == 200
    assert (
        client.get(f"{base}/parse-runs/{run.id}/raw", headers=auth(credentials, 1)).status_code
        == 403
    )
    raw = client.get(f"{base}/parse-runs/{run.id}/raw", headers=auth(credentials))
    assert raw.status_code == 200

    # Another tenant sees nothing, even with the exact resource UUIDs.
    for path in (
        f"{base}/parse",
        f"{base}/parse-runs/{run.id}",
        f"{base}/parse-runs/{run.id}/pages",
        f"{base}/parse-runs/{run.id}/pages/{page_id}/preview",
        f"{base}/parse-runs/{run.id}/tables/{table_id}",
    ):
        assert client.get(path, headers=auth(credentials, 2)).status_code == 404
    assert client.get(f"{base}/parse").status_code == 401


def test_no_storage_location_is_ever_exposed_by_the_api(parsed_table):
    client, control, credentials, body, _ = parsed_table
    run = run_of(control, body["version_id"])
    base = f"/api/v1/documents/{body['document_id']}/versions/{body['version_id']}"
    for path in (
        f"{base}/parse",
        f"{base}/parse-runs/{run.id}",
        f"{base}/parse-runs/{run.id}/pages",
        f"{base}/parse-runs/{run.id}/figures",
    ):
        payload = client.get(path, headers=auth(credentials)).text
        for leaked in ("preview_key", "image_key", "raw_artifact_key", "9000", "minio"):
            assert leaked not in payload


def test_duplicate_delivery_never_creates_a_second_parse(parsed_table):
    """Redelivery of the same outbox message must not produce a second normalized dataset."""
    _, control, _, body, _ = parsed_table
    with control.sessions() as session:
        message = session.scalar(
            select(OutboxMessage).where(OutboxMessage.job_id == uuid_of(body["job_id"]))
        )
    assert receive(control.sessions, control.storage, message.id) is None
    service = make_service(control, parser_impl=Recorded(RuntimeError("must not be called")))
    outcome = service.run(uuid_of(body["job_id"]))
    assert outcome.skipped == "not_queued"
    with control.sessions() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(ParseRun).where(
                            ParseRun.document_version_id == uuid_of(body["version_id"])
                        )
                    )
                )
            )
            == 1
        )


# --------------------------------------------------------------------- structural fixtures


@pytest.mark.parametrize(
    "fixture,expectation",
    [
        ("multi-page.pdf", "headers"),
        ("figure.pdf", "figure"),
        ("formula.pdf", "formula"),
        ("two-column.pdf", "columns"),
        ("scanned-like.pdf", "ocr"),
    ],
)
def test_structural_fidelity(system_module, fixture, expectation):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, fixture)
    outcome = make_service(control).run(uuid_of(body["job_id"]))
    assert outcome.status is Status.READY_FOR_CHUNKING, f"{fixture}: {outcome}"
    run = run_of(control, body["version_id"])
    with control.sessions() as session:
        elements = list(
            session.scalars(
                select(DocumentElement)
                .where(DocumentElement.parse_run_id == run.id)
                .order_by(DocumentElement.reading_order)
            )
        )
        pages = list(
            session.scalars(
                select(DocumentPage)
                .where(DocumentPage.parse_run_id == run.id)
                .order_by(DocumentPage.page_number)
            )
        )
        figures = list(
            session.scalars(select(FigureArtifact).where(FigureArtifact.parse_run_id == run.id))
        )
        formulas = list(
            session.scalars(select(FormulaArtifact).where(FormulaArtifact.parse_run_id == run.id))
        )

    if expectation == "headers":
        assert [page.page_number for page in pages] == [1, 2, 3]
        for number in (1, 2, 3):
            page_text = pages[number - 1].extracted_text
            assert f"ALPHA-{number}-OMEGA" in page_text
            # Running heads and feet are preserved, never silently dropped from the page.
            assert "Synthetic Parsing Handbook" in page_text
            assert f"Educational fixture | page {number}" in page_text
        order = [(element.page_number, element.reading_order) for element in elements]
        assert order == sorted(order)  # reading order never regresses across pages here
    if expectation == "figure":
        assert figures
        located = [figure for figure in figures if figure.image_key]
        assert located, "no figure image artifact was stored"
        for figure in located:
            assert figure.image_bytes and figure.image_media_type == "image/png"
            assert figure.page_number == 1 and figure.bbox_x1 is not None
            stored = control.storage.read(figure.image_key, figure.image_version_id)
            assert len(stored) == figure.image_bytes
        # A caption is recorded only when the parser declares the relation; the two columns are
        # written together, so one can never be present without the other.
        for figure in figures:
            assert (figure.caption_element_id is None) == (figure.caption_text is None)
        assert "Synthetic diagram caption" in pages[0].extracted_text
    if expectation == "formula":
        # Whether this displayed equation is classified as a formula is a layout-model
        # judgement that differs between CPU environments, so the portable assertion is that
        # the surrounding content survives and any formula found is stored faithfully.
        assert "Equation 1 defines" in pages[0].extracted_text
        for formula in formulas:
            assert formula.normalized_expression
            # Deterministic whitespace normalization only; the notation itself is untouched.
            assert formula.normalized_expression.replace(" ", "") == (
                (formula.source_expression or "").replace(" ", "")
            )
            element = next(e for e in elements if e.id == formula.document_element_id)
            assert element.element_type is ElementType.FORMULA
    if expectation == "columns":
        text = pages[0].extracted_text
        assert text.index("LEFT-1") < text.index("RIGHT-1")
        assert text.index("LEFT-3") < text.index("RIGHT-1")  # column-wise, not row-wise
    if expectation == "ocr":
        assert pages[0].source_text_chars == 0  # no text layer at all
        assert pages[0].ocr_used and pages[0].ocr_evidence == "no_source_text_layer"
        assert run.ocr_engine == "rapidocr" and run.ocr_page_count == 1
        assert "SCANNED" in pages[0].extracted_text.upper()


def test_continuation_is_flagged_without_merging_tables(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "continued-table.pdf")
    assert make_service(control).run(uuid_of(body["job_id"])).status is Status.READY_FOR_CHUNKING
    run = run_of(control, body["version_id"])
    with control.sessions() as session:
        tables = sorted(
            session.scalars(select(TableArtifact).where(TableArtifact.parse_run_id == run.id)),
            key=lambda table: table.page_number,
        )
    assert len(tables) == 2  # never destructively merged
    assert tables[0].page_number == 1 and tables[1].page_number == 2
    assert tables[0].column_count == tables[1].column_count == 3
    assert not tables[0].possible_continuation  # the first part is never a continuation
    # The continuation rule needs the parser to have identified a header row on the first part.
    # When it does, the relation must be fully recorded; when it does not, nothing is invented.
    if tables[1].possible_continuation:
        assert tables[1].continuation_of_id == tables[0].id
        assert tables[1].continuation_evidence == "adjacent_page_same_columns"
        assert tables[0].table_group_id == tables[1].table_group_id
    else:
        assert tables[1].continuation_of_id is None
        assert tables[1].continuation_evidence is None


def test_question_bank_structure_is_preserved_without_being_interpreted(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "question-bank.pdf")
    assert make_service(control).run(uuid_of(body["job_id"])).status is Status.READY_FOR_CHUNKING
    run = run_of(control, body["version_id"])
    with control.sessions() as session:
        elements = list(
            session.scalars(
                select(DocumentElement)
                .where(DocumentElement.parse_run_id == run.id)
                .order_by(DocumentElement.reading_order)
            )
        )
    texts = [element.normalized_text or "" for element in elements]
    page_text = page_text_of(control, run.id, 1)
    assert any(text.startswith("Answer:") for text in texts)
    assert any(text.startswith("Explanation:") for text in texts)
    for option in ("First", "Second", "Third", "Fourth"):
        assert f"{option} synthetic option" in page_text
    # M2 stores structural cues only: no question object, key or answer is derived here.
    assert not any(element.structure_inferred for element in elements)


# --------------------------------------------------------------------- failure paths


def parsed_stub(pages=1):
    from app.ingestion.parser.model import ParsedDocument, ParsedElement, ParsedPage

    return ParsedDocument(
        parser_name="docling",
        parser_provider="docling-project",
        parser_version="test-parser",
        pages=tuple(
            ParsedPage(page_number=n, width=612, height=792, source_text_chars=500)
            for n in range(1, pages + 1)
        ),
        elements=tuple(
            ParsedElement(
                reference=f"#/texts/{n}",
                element_type=ElementType.PARAGRAPH,
                reading_order=n,
                ordinal=n,
                depth=1,
                page_number=1,
                text="Synthetic paragraph content long enough to clear the empty-page floor.",
            )
            for n in range(3)
        ),
        raw_artifact=json.dumps({"artifact_schema": "medrag.parse.raw/1"}).encode(),
        source_page_count=pages,
    )


def test_missing_source_fails_closed(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    with control.sessions() as session:
        version = session.get(DocumentVersion, uuid_of(body["version_id"]))
        key = version.object_storage_key
    control.storage.delete(key)
    outcome = make_service(control, parser_impl=Recorded(parsed_stub())).run(
        uuid_of(body["job_id"])
    )
    assert outcome.status is Status.FAILED
    run = run_of(control, body["version_id"])
    assert run.status is ParseRunStatus.FAILED and not run.is_active
    assert run.error_code == PARSER_SOURCE_MISSING
    with control.sessions() as session:
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
        assert job.status is Status.FAILED
        assert job.last_error_code == PARSER_SOURCE_MISSING
        assert "docling" not in (job.last_error_message or "").lower()


def test_corrupt_pdf_is_not_retried_forever(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    error = ParserError(PARSER_SOURCE_CORRUPT)
    outcome = make_service(control, parser_impl=Recorded(error)).run(uuid_of(body["job_id"]))
    assert outcome.status is Status.FAILED
    run = run_of(control, body["version_id"])
    assert run.error_code == PARSER_SOURCE_CORRUPT
    assert not error.retryable  # deterministic input defect: no automatic retry


def test_unexpected_parser_exception_never_leaks_vendor_detail(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    secret = RuntimeError("docling internal state 0xdeadbeef /var/lib/models/weights.bin")
    outcome = make_service(control, parser_impl=Recorded(secret)).run(uuid_of(body["job_id"]))
    assert outcome.status is Status.FAILED
    run = run_of(control, body["version_id"])
    assert run.error_code == "PARSER_INTERNAL_ERROR"
    assert "0xdeadbeef" not in run.error_message and "weights.bin" not in run.error_message


def test_raw_artifact_storage_failure_fails_the_run(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")

    class BrokenStorage:
        def __getattr__(self, name):
            return getattr(control.storage, name)

        def put_bytes(self, *args, **kwargs):
            raise OSError("object storage unavailable")

    service = ParseService(
        control.sessions,
        BrokenStorage(),
        Recorded(parsed_stub()),
        ParsingConfig(),
        ParseMetrics(CollectorRegistry()),
    )
    assert service.run(uuid_of(body["job_id"])).status is Status.FAILED
    run = run_of(control, body["version_id"])
    assert run.error_code == RAW_ARTIFACT_STORAGE_FAILED
    assert run.raw_artifact_key is None
    with control.sessions() as session:
        assert not list(
            session.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == run.id))
        )


def test_validation_failure_routes_to_needs_review_and_persists_findings(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    # A threshold no real page can meet: the parse succeeds but the quality layer rejects it.
    config = ParsingConfig(thresholds=ParseThresholds(min_chars_per_page=10000))
    outcome = make_service(control, config=config).run(uuid_of(body["job_id"]))
    assert outcome.status is Status.NEEDS_REVIEW
    assert outcome.result is ParseResult.NEEDS_REVIEW
    run = run_of(control, body["version_id"])
    assert run.status is ParseRunStatus.FAILED and not run.is_active
    assert run.validation_result is ParseResult.NEEDS_REVIEW
    with control.sessions() as session:
        findings = list(
            session.scalars(
                select(ParseValidationFinding).where(ParseValidationFinding.parse_run_id == run.id)
            )
        )
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
        version = session.get(DocumentVersion, uuid_of(body["version_id"]))
    assert findings and {finding.severity for finding in findings} & {
        Severity.WARNING,
        Severity.ERROR,
    }
    assert job.status is Status.NEEDS_REVIEW and version.searchable is False
    # Suspicious output must never present itself as an active parse dataset.
    assert run.is_active is False


def test_findings_are_append_only(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    config = ParsingConfig(thresholds=ParseThresholds(min_chars_per_page=10000))
    make_service(control, config=config).run(uuid_of(body["job_id"]))
    run = run_of(control, body["version_id"])
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        finding = session.scalar(
            select(ParseValidationFinding).where(ParseValidationFinding.parse_run_id == run.id)
        )
        finding.message = "rewritten"
        session.flush()


def test_cancelled_job_releases_the_parse_run(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")

    class CancellingParser(Recorded):
        def parse(self, source, config):
            client.post(
                f"/api/v1/ingestion/jobs/{body['job_id']}/cancel", headers=auth(credentials)
            )
            return parsed_stub()

    outcome = make_service(control, parser_impl=CancellingParser(None)).run(uuid_of(body["job_id"]))
    assert outcome.skipped == "cancelled"
    run = run_of(control, body["version_id"])
    assert run.status is ParseRunStatus.CANCELLED and not run.is_active
    with control.sessions() as session:
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
        assert job.status is Status.CANCELLED
        assert not list(
            session.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == run.id))
        )


def test_expired_lease_is_released_so_a_crashed_parse_can_be_retried(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    config = ParsingConfig(lease_seconds=60)
    service = make_service(control, config=config)
    claim = service._claim(uuid_of(body["job_id"]), None)  # simulate a worker that then dies
    assert claim.status is Status.PARSING
    with control.sessions.begin() as session:
        from datetime import UTC, datetime, timedelta

        run = session.get(ParseRun, claim.parse_run_id)
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert reap_expired_leases(control.sessions, config) == 1
    run = run_of(control, body["version_id"])
    assert run.status is ParseRunStatus.FAILED and run.error_code == "PARSER_LEASE_EXPIRED"
    with control.sessions() as session:
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
    assert job.status is Status.FAILED
    # Bounded retry is available: the operator path forward is not blocked.
    retried = client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/retry", headers=auth(credentials)
    )
    assert retried.status_code == 200 and retried.json()["status"] == "QUEUED"


def test_reparse_after_config_change_supersedes_without_destroying_the_previous_run(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    first = make_service(control, parser_impl=Recorded(parsed_stub())).run(uuid_of(body["job_id"]))
    assert first.status is Status.READY_FOR_CHUNKING
    original = run_of(control, body["version_id"])

    # A forced reparse is explicit and authorized; nothing reparses on its own.
    requested = client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/reparse", headers=auth(credentials)
    )
    assert requested.status_code == 200 and requested.json()["status"] == "QUEUED"
    assert requested.json()["retry_count"] == 1
    assert client.post(
        f"/api/v1/ingestion/jobs/{body['job_id']}/reparse", headers=auth(credentials, 1)
    ).status_code in {403, 409}
    changed = ParsingConfig(version="parsing-m2-v2", ocr_mode=OcrMode.OFF)
    second = make_service(
        control, config=changed, parser_impl=Recorded(parsed_stub(), version="test-parser-2")
    ).run(uuid_of(body["job_id"]))
    assert second.status is Status.READY_FOR_CHUNKING
    assert second.parse_run_id != original.id

    with control.sessions() as session:
        runs = sorted(
            session.scalars(
                select(ParseRun).where(ParseRun.document_version_id == uuid_of(body["version_id"]))
            ),
            key=lambda run: run.attempt,
        )
        assert len(runs) == 2 and [run.attempt for run in runs] == [1, 2]
        assert runs[0].is_active is False and runs[1].is_active is True
        # The superseded run keeps its own rows and artifact for comparison.
        assert runs[0].raw_artifact_key != runs[1].raw_artifact_key
        assert list(
            session.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == runs[0].id))
        )
        assert runs[0].configuration_version == "parsing-m2-v1"
        assert runs[1].configuration_version == "parsing-m2-v2"


def test_identical_parser_and_policy_is_idempotent(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    stub = Recorded(parsed_stub())
    assert make_service(control, parser_impl=stub).run(uuid_of(body["job_id"])).status is (
        Status.READY_FOR_CHUNKING
    )
    assert stub.calls == 1
    assert (
        client.post(
            f"/api/v1/ingestion/jobs/{body['job_id']}/reparse", headers=auth(credentials)
        ).status_code
        == 200
    )
    outcome = make_service(control, parser_impl=stub).run(uuid_of(body["job_id"]))
    assert outcome.skipped == "already_parsed"
    assert stub.calls == 1  # the parser was not run a second time
    with control.sessions() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(ParseRun).where(
                            ParseRun.document_version_id == uuid_of(body["version_id"])
                        )
                    )
                )
            )
            == 1
        )
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
    assert job.status is Status.READY_FOR_CHUNKING


def test_only_one_active_parse_run_per_version_is_possible(system_module):
    """The database, not only the application, enforces a single active parse dataset."""
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    make_service(control, parser_impl=Recorded(parsed_stub())).run(uuid_of(body["job_id"]))
    existing = run_of(control, body["version_id"])
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), control.sessions.begin() as session:
        session.add(
            ParseRun(
                id=uuid4(),
                tenant_id=existing.tenant_id,
                document_version_id=existing.document_version_id,
                attempt=99,
                parser_name="docling",
                parser_provider="docling-project",
                parser_version="x",
                configuration_version="x",
                configuration_fingerprint="x",
                config_snapshot={},
                source_checksum="x",
                source_object_version_id="x",
                status=ParseRunStatus.SUCCEEDED,
                is_active=True,
                ocr_mode=OcrMode.OFF,
                tables_enabled=True,
                formulas_enabled=True,
                figures_enabled=True,
                previews_enabled=True,
                correlation_id=uuid4(),
            )
        )
        session.flush()


def test_database_rejects_a_transition_that_skips_a_parse_stage(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError), control.sessions.begin() as session:
        job = session.get(IngestionJob, uuid_of(body["job_id"]))
        job.status = Status.READY_FOR_CHUNKING  # QUEUED -> READY_FOR_CHUNKING is not a real edge
        session.flush()


def test_dispatcher_cycle_still_publishes_and_reaps(system_module):
    """The dispatcher gained lease reaping without losing its M1 outbox responsibility."""
    client, control, credentials = system_module
    response = upload(client, credentials, file="basic-text.pdf", content=unique("basic-text.pdf"))
    assert response.status_code == 201

    class Publisher:
        def __init__(self):
            self.sent = []

        def publish(self, message_id):
            self.sent.append(message_id)

    publisher = Publisher()
    sent = dispatch(control.sessions, publisher, control.settings.ingestion)
    assert sent >= 1 and publisher.sent
    assert reap_expired_leases(control.sessions, ParsingConfig()) == 0


# --------------------------------------------------------------------- deterministic artifacts


def artifact_stub():
    """A parsed document containing every artifact kind, with parser-declared relationships.

    Real layout prediction differs between CPU environments, so the artifact, caption and
    provenance pipeline is exercised here against a fixed parser output. What is asserted is
    entirely our own behaviour: persistence, coordinate storage, object-storage artifacts and
    the authorized API.
    """
    from app.ingestion.parser.model import (
        BoundingBox,
        ParsedDocument,
        ParsedElement,
        ParsedFigure,
        ParsedFormula,
        ParsedPage,
        ParsedTable,
        ParsedTableCell,
    )

    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00"
        b"\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r"
        b"\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    box = BoundingBox(x1=72.0, y1=100.0, x2=300.0, y2=140.0)
    expression = "C  =  \\frac { A \\cdot B } { D }"
    return ParsedDocument(
        parser_name="docling",
        parser_provider="docling-project",
        parser_version="test-parser",
        pages=(ParsedPage(page_number=1, width=612, height=792, source_text_chars=400),),
        elements=(
            ParsedElement(
                reference="#/texts/0",
                element_type=ElementType.PARAGRAPH,
                reading_order=0,
                ordinal=0,
                depth=1,
                page_number=1,
                text="Introductory paragraph long enough to clear the empty-page floor easily.",
                bbox=box,
            ),
            ParsedElement(
                reference="#/tables/0",
                element_type=ElementType.TABLE,
                reading_order=1,
                ordinal=1,
                depth=1,
                page_number=1,
                bbox=box,
                caption_references=("#/texts/1",),
                table=ParsedTable(
                    row_count=2,
                    column_count=2,
                    header_row_count=1,
                    cells=(
                        ParsedTableCell(text="Parameter", row=0, column=0, is_column_header=True),
                        ParsedTableCell(text="Value", row=0, column=1, is_column_header=True),
                        ParsedTableCell(text="Alpha", row=1, column=0),
                        ParsedTableCell(text="10", row=1, column=1),
                    ),
                    markdown="| Parameter | Value |",
                ),
            ),
            ParsedElement(
                reference="#/texts/1",
                element_type=ElementType.CAPTION,
                reading_order=2,
                ordinal=0,
                depth=2,
                page_number=1,
                parent_reference="#/tables/0",
                text="Table 1. Stub table caption.",
                bbox=box,
            ),
            ParsedElement(
                reference="#/pictures/0",
                element_type=ElementType.FIGURE,
                reading_order=3,
                ordinal=2,
                depth=1,
                page_number=1,
                bbox=box,
                caption_references=("#/texts/2",),
                figure=ParsedFigure(
                    image=png, media_type="image/png", width=1, height=1, kind="picture"
                ),
            ),
            ParsedElement(
                reference="#/texts/2",
                element_type=ElementType.CAPTION,
                reading_order=4,
                ordinal=0,
                depth=2,
                page_number=1,
                parent_reference="#/pictures/0",
                text="Figure 1. Stub figure caption.",
                bbox=box,
            ),
            ParsedElement(
                reference="#/texts/3",
                element_type=ElementType.FORMULA,
                reading_order=5,
                ordinal=3,
                depth=1,
                page_number=1,
                bbox=box,
                text=expression,
                formula=ParsedFormula(source_expression=expression, notation="latex"),
            ),
            ParsedElement(
                reference="#/texts/4",
                element_type=ElementType.PARAGRAPH,
                reading_order=6,
                ordinal=4,
                depth=1,
                page_number=1,
                text="Where A, B and D are synthetic quantities with no clinical meaning.",
                bbox=box,
            ),
        ),
        raw_artifact=json.dumps({"artifact_schema": "medrag.parse.raw/1"}).encode(),
        source_page_count=1,
    )


def test_every_artifact_kind_is_persisted_with_its_relationships(system_module):
    client, control, credentials = system_module
    body, _ = queue_job(client, control, credentials, "basic-text.pdf")
    outcome = make_service(control, parser_impl=Recorded(artifact_stub())).run(
        uuid_of(body["job_id"])
    )
    assert outcome.status is Status.READY_FOR_CHUNKING
    run = run_of(control, body["version_id"])
    assert (run.table_count, run.figure_count, run.formula_count) == (1, 1, 1)

    with control.sessions() as session:
        elements = {
            element.source_parser_ref: element
            for element in session.scalars(
                select(DocumentElement).where(DocumentElement.parse_run_id == run.id)
            )
        }
        table = session.scalar(select(TableArtifact).where(TableArtifact.parse_run_id == run.id))
        figure = session.scalar(select(FigureArtifact).where(FigureArtifact.parse_run_id == run.id))
        formula = session.scalar(
            select(FormulaArtifact).where(FormulaArtifact.parse_run_id == run.id)
        )
        page = session.scalar(select(DocumentPage).where(DocumentPage.parse_run_id == run.id))

    # Hierarchy comes from the parser's own containment, never from geometry.
    assert elements["#/texts/1"].parent_element_id == elements["#/tables/0"].id
    assert elements["#/texts/2"].parent_element_id == elements["#/pictures/0"].id
    assert elements["#/texts/0"].parent_element_id is None

    # Coordinates are stored once, in the documented TOPLEFT/point convention.
    located = elements["#/texts/0"]
    assert (located.bbox_x1, located.bbox_y1, located.bbox_x2, located.bbox_y2) == (
        72.0,
        100.0,
        300.0,
        140.0,
    )
    assert located.bbox_origin.value == "TOPLEFT"

    assert table.caption_element_id == elements["#/texts/1"].id
    assert table.caption_text == "Table 1. Stub table caption."
    assert table.row_count == 2 and table.column_count == 2 and table.header_row_count == 1
    assert len(table.cells) == 4 and table.markdown == "| Parameter | Value |"
    assert not table.malformed

    assert figure.caption_element_id == elements["#/texts/2"].id
    assert figure.caption_text == "Figure 1. Stub figure caption."
    assert figure.image_key and figure.image_media_type == "image/png"
    assert control.storage.read(figure.image_key, figure.image_version_id)[:4] == b"\x89PNG"

    # Formula text is only whitespace-normalized, and its neighbouring prose is linked.
    assert formula.source_expression == "C  =  \\frac { A \\cdot B } { D }"
    assert formula.normalized_expression == "C = \\frac { A \\cdot B } { D }"
    assert formula.notation == "latex"
    assert formula.following_element_id == elements["#/texts/4"].id
    assert formula.preceding_element_id is None  # a figure caption is not explanatory prose

    # The page rollup renders artifacts that carry no element text of their own.
    assert "| Parameter | Value |" in page.extracted_text
    assert "\\frac" in page.extracted_text
    assert page.element_count == 7

    base = f"/api/v1/documents/{body['document_id']}/versions/{body['version_id']}"
    headers = auth(credentials)
    figures = client.get(f"{base}/parse-runs/{run.id}/figures", headers=headers).json()
    assert figures["items"][0]["has_image"] and figures["items"][0]["bbox"]["origin"] == "TOPLEFT"
    image = client.get(f"{base}/parse-runs/{run.id}/figures/{figure.id}/image", headers=headers)
    assert image.status_code == 200 and image.content[:4] == b"\x89PNG"
    assert image.headers["cache-control"] == "no-store"
    formulas = client.get(f"{base}/parse-runs/{run.id}/formulas", headers=headers).json()
    assert formulas["items"][0]["normalized_expression"] == "C = \\frac { A \\cdot B } { D }"
    detail = client.get(f"{base}/parse-runs/{run.id}/tables/{table.id}", headers=headers).json()
    assert detail["caption_text"] == "Table 1. Stub table caption."
    assert len(detail["cells"]) == 4
