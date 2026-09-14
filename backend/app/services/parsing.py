"""M2 parse pipeline.

Ordering guarantees, in this order and no other:

  QUEUED -> PARSING     lease acquired under the job row lock, ParseRun row created RUNNING
         -> (parse)     raw parser artifact written to object storage and pinned on the run
         -> NORMALIZING pages, elements, tables, figures, formulas persisted in one transaction
         -> ENRICHING   deterministic enrichment: captions, continuations, page text, counters
         -> (validate)  quality rules produce findings and one structured result
         -> READY_FOR_CHUNKING, NEEDS_REVIEW or FAILED

A ParseRun only becomes `is_active` in the same transaction that reaches READY_FOR_CHUNKING, so a
crash at any earlier point leaves diagnostic rows behind without ever exposing partial output as
valid parse data. No model provider is called anywhere in this module.
"""

import logging
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.parsing_config import ParsingConfig
from app.ingestion.normalizer.text import changed, normalize_expression, normalize_text, page_text
from app.ingestion.parser.errors import (
    FIGURE_ARTIFACT_STORAGE_FAILED,
    PARSE_PERSISTENCE_FAILED,
    PARSER_INTERNAL_ERROR,
    PARSER_SOURCE_CORRUPT,
    PARSER_SOURCE_MISSING,
    RAW_ARTIFACT_STORAGE_FAILED,
    ParserError,
    safe_message,
)
from app.ingestion.parser.model import (
    ParsedDocument,
    ParsedElement,
    ParserConfig,
    ParseSource,
)
from app.ingestion.state import transition
from app.ingestion.validation import text_recovery
from app.ingestion.validation.parse_quality import (
    ElementFacts,
    FigureFacts,
    FormulaFacts,
    PageFacts,
    ParseFacts,
    TableFacts,
    validate,
)
from app.models.documents import Document, DocumentVersion, IngestionJob
from app.models.enums import ElementType, ParseResult, ParseRunStatus, Status
from app.models.parsing import (
    DocumentElement,
    DocumentPage,
    FigureArtifact,
    FormulaArtifact,
    ParseRun,
    ParseValidationFinding,
    TableArtifact,
)
from app.observability.ingestion import audit, phase_event
from app.observability.parsing import ParseMetrics
from app.services.storage import (
    ObjectStorage,
    figure_key,
    page_preview_key,
    raw_parse_key,
)

logger = logging.getLogger("medical_rag.requests")

# A finished parse maps its structured result onto exactly one ingestion outcome.
RESULT_STATUS: dict[ParseResult, Status] = {
    ParseResult.PASS: Status.READY_FOR_CHUNKING,
    ParseResult.PASS_WITH_WARNINGS: Status.READY_FOR_CHUNKING,
    ParseResult.NEEDS_REVIEW: Status.NEEDS_REVIEW,
    ParseResult.FAIL: Status.FAILED,
}


@dataclass(frozen=True)
class ParseOutcome:
    parse_run_id: UUID | None
    status: Status | None
    result: ParseResult | None
    skipped: str | None = None


class ParseCancelled(Exception):
    """The job left the parse path (cancel or archive) while this attempt was running."""


class ParseService:
    """Owns the durable parse lifecycle. Construct one per worker process."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        storage: ObjectStorage,
        parser: Any,
        config: ParsingConfig,
        metrics: ParseMetrics,
        worker_identity: str = "celery-parser",
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.parser = parser
        self.config = config
        self.metrics = metrics
        self.worker_identity = worker_identity

    # ------------------------------------------------------------------ public entry

    def run(self, job_id: UUID, correlation_id: UUID | None = None) -> ParseOutcome:
        claim = self._claim(job_id, correlation_id)
        if claim.skipped:
            return claim
        assert claim.parse_run_id is not None
        run_id = claim.parse_run_id
        started = time.perf_counter()
        self.metrics.started.inc()
        try:
            with self._workspace() as workspace:
                parsed = self._parse(run_id, workspace)
                self._persist(run_id, parsed)
                outcome = self._enrich_and_validate(run_id, parsed, workspace / "source.pdf")
        except ParseCancelled:
            self._release_cancelled(run_id)
            self.metrics.cancelled.inc()
            return ParseOutcome(run_id, Status.CANCELLED, None, skipped="cancelled")
        except ParserError as exc:
            self._fail(run_id, exc.code, exc.message)
            self.metrics.failed.labels(code=exc.code).inc()
            return ParseOutcome(run_id, Status.FAILED, None)
        except Exception as exc:  # Never leak a vendor traceback into durable state.
            logger.error(
                "parse_unhandled_error",
                extra={"event": "parse_unhandled_error", "error_type": type(exc).__name__},
            )
            self._fail(run_id, PARSER_INTERNAL_ERROR, safe_message(PARSER_INTERNAL_ERROR))
            self.metrics.failed.labels(code=PARSER_INTERNAL_ERROR).inc()
            return ParseOutcome(run_id, Status.FAILED, None)
        self.metrics.duration.observe(time.perf_counter() - started)
        return outcome

    # ------------------------------------------------------------------ lease / claim

    def _claim(self, job_id: UUID, correlation_id: UUID | None) -> ParseOutcome:
        """Acquire the parse lease for a QUEUED job and create its RUNNING ParseRun.

        Duplicate Celery delivery, a redelivered outbox message and a concurrent worker all
        converge here: only the transaction that observes the job still QUEUED under a row lock
        can move it to PARSING, so at most one active parse exists per job generation.
        """
        with self.sessions.begin() as session:
            job = session.get(IngestionJob, job_id, with_for_update=True)
            if job is None:
                return ParseOutcome(None, None, None, skipped="job_missing")
            version = session.get(DocumentVersion, job.document_version_id)
            if version is None:
                return ParseOutcome(None, None, None, skipped="version_missing")
            document = session.get(Document, version.document_id)
            if document is None or document.archived_at:
                return ParseOutcome(None, None, None, skipped="document_archived")
            if job.status != Status.QUEUED:
                return ParseOutcome(None, job.status, None, skipped="not_queued")
            existing = session.scalar(
                select(ParseRun).where(
                    ParseRun.document_version_id == version.id,
                    ParseRun.is_active.is_(True),
                    ParseRun.configuration_fingerprint == self.config.fingerprint,
                    ParseRun.parser_version == getattr(self.parser, "version", "unknown"),
                    ParseRun.source_checksum == version.sha256,
                )
            )
            if existing is not None:
                # Same version, same parser build, same policy, same bytes: reparsing would
                # produce the same normalized dataset. Move the job forward without duplicating.
                self._advance_to_ready(session, job, version, existing)
                return ParseOutcome(
                    existing.id, Status.READY_FOR_CHUNKING, None, skipped="already_parsed"
                )
            if correlation_id is not None:
                job.correlation_id = correlation_id
            attempt = (
                session.scalar(
                    select(ParseRun.attempt)
                    .where(ParseRun.document_version_id == version.id)
                    .order_by(ParseRun.attempt.desc())
                    .limit(1)
                )
                or 0
            ) + 1
            now = datetime.now(UTC)
            run = ParseRun(
                id=uuid4(),
                tenant_id=version.tenant_id,
                document_version_id=version.id,
                ingestion_job_id=job.id,
                attempt=attempt,
                parser_name=getattr(self.parser, "name", self.config.parser_name),
                parser_provider=getattr(self.parser, "provider", "unknown"),
                parser_version=getattr(self.parser, "version", "unknown"),
                configuration_version=self.config.version,
                configuration_fingerprint=self.config.fingerprint,
                config_snapshot=self.config.model_dump(mode="json"),
                source_checksum=version.sha256,
                source_object_version_id=version.object_version_id,
                status=ParseRunStatus.RUNNING,
                is_active=False,
                ocr_mode=self.config.ocr_mode,
                tables_enabled=self.config.extract_tables,
                formulas_enabled=self.config.extract_formulas,
                figures_enabled=self.config.extract_figures,
                previews_enabled=self.config.generate_page_previews,
                worker_identity=self.worker_identity,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=self.config.lease_seconds),
                started_at=now,
                correlation_id=job.correlation_id,
            )
            session.add(run)
            transition(session, job, version, Status.PARSING, None, service=self.worker_identity)
            audit(
                session,
                version.tenant_id,
                None,
                "PARSE_RUN_STARTED",
                run.id,
                job.correlation_id,
                {
                    "attempt": attempt,
                    "parser": run.parser_name,
                    "parser_version": run.parser_version,
                    "config_version": run.configuration_version,
                },
            )
            phase_event("PARSE_RUN_STARTED", job.correlation_id, run.id)
            return ParseOutcome(run.id, Status.PARSING, None)

    @contextmanager
    def _workspace(self) -> Iterator[Path]:
        """Per-job temporary directory, removed on success, failure and cancellation alike."""
        base = self.config.temp_dir
        with tempfile.TemporaryDirectory(prefix="medrag-parse-", dir=base) as directory:
            yield Path(directory)

    # ------------------------------------------------------------------ stage: PARSING

    def _parse(self, run_id: UUID, workspace: Path) -> ParsedDocument:
        with self.sessions() as session:
            run = self._require_run(session, run_id)
            version = session.get(DocumentVersion, run.document_version_id)
            if version is None:
                raise ParserError(PARSER_SOURCE_MISSING)
            key, object_version = version.object_storage_key, version.object_version_id
            document_id, version_id = version.document_id, version.id
            filename = version.normalized_filename
            checksum = version.sha256

        local = workspace / "source.pdf"
        try:
            with local.open("wb") as handle:
                self.storage.download(key, object_version, handle)
        except Exception:
            raise ParserError(PARSER_SOURCE_MISSING) from None
        if local.stat().st_size != 0 and local.stat().st_size < 8:
            raise ParserError(PARSER_SOURCE_CORRUPT)

        def renew(done: int, total: int) -> None:
            """Renew the parse lease between page windows.

            Without this the lease is only renewed at stage boundaries, so a worker killed
            during a long conversion leaves the job showing PARSING for the whole lease period
            before the dispatcher's reaper can move it to a recoverable failure. Beating between
            windows makes the lease reflect liveness rather than a worst-case duration.
            """
            with self.sessions.begin() as beat_session:
                beating = beat_session.get(ParseRun, run_id)
                if beating is not None:
                    self._beat(beating)
            logger.info(
                "parse_progress",
                extra={
                    "event": "PARSE_WINDOW_COMPLETED",
                    "resource_id": str(run_id),
                    "status": f"{done}/{total}",
                },
            )

        parsed: ParsedDocument = self.parser.parse(
            ParseSource(path=local, sha256=checksum, filename=filename),
            ParserConfig(
                ocr_mode=self.config.ocr_mode.value,
                extract_tables=self.config.extract_tables,
                extract_formulas=self.config.extract_formulas,
                extract_figures=self.config.extract_figures,
                generate_page_previews=self.config.generate_page_previews,
                preview_scale=self.config.preview_scale,
                timeout_seconds=self.config.timeout_seconds,
                max_pages=self.config.max_pages,
                preview_format=self.config.preview_format,
                figure_format=self.config.figure_format,
                threads=self.config.parser_threads,
                temp_dir=workspace,
                page_window_size=self.config.page_window_size,
                page_window_threshold=self.config.page_window_threshold,
                document_seconds_per_page=self.config.document_seconds_per_page,
                max_document_timeout_seconds=self.config.max_document_timeout_seconds,
            ),
            on_progress=renew,
        )
        if len(parsed.raw_artifact) > self.config.max_artifact_bytes:
            raise ParserError(RAW_ARTIFACT_STORAGE_FAILED, "artifact_too_large")
        artifact_key = raw_parse_key(document_id, version_id, run_id)
        try:
            stat = self.storage.put_bytes(
                artifact_key, parsed.raw_artifact, parsed.raw_artifact_media_type
            )
        except Exception:
            raise ParserError(RAW_ARTIFACT_STORAGE_FAILED) from None
        with self.sessions.begin() as session:
            run = self._require_run(session, run_id)
            run.raw_artifact_key = artifact_key
            run.raw_artifact_version_id = stat.version_id
            run.raw_artifact_bytes = stat.size
            run.ocr_engine = parsed.ocr_engine
            run.duration_ms = parsed.duration_ms
            self._beat(run)
        phase_event("PARSE_RAW_ARTIFACT_STORED", self._correlation(run_id), run_id)
        return parsed

    # ------------------------------------------------------------------ stage: NORMALIZING

    def _persist(self, run_id: UUID, parsed: ParsedDocument) -> None:
        """Write pages, elements and artifacts in a single transaction.

        Figure images and page previews are uploaded before the transaction commits; a storage
        failure for a figure fails the run rather than persisting an element that claims an
        artifact which does not exist.
        """
        with self.sessions.begin() as session:
            run = self._require_run(session, run_id)
            job = session.get(IngestionJob, run.ingestion_job_id) if run.ingestion_job_id else None
            version = session.get(DocumentVersion, run.document_version_id)
            if version is None:
                raise ParserError(PARSER_SOURCE_MISSING)
            if job is None or job.status != Status.PARSING:
                raise ParseCancelled
            transition(
                session, job, version, Status.NORMALIZING, None, service=self.worker_identity
            )
            document_id = version.document_id

            page_rows: dict[int, DocumentPage] = {}
            for page in parsed.pages:
                preview_key = preview_version = preview_type = None
                if page.preview_image:
                    preview_key = page_preview_key(
                        document_id,
                        version.id,
                        run_id,
                        page.page_number,
                        self.config.preview_format,
                    )
                    try:
                        stat = self.storage.put_bytes(
                            preview_key,
                            page.preview_image,
                            page.preview_media_type or "application/octet-stream",
                        )
                        preview_version = stat.version_id
                        preview_type = page.preview_media_type
                    except Exception:
                        # A preview is an optional debugging aid; structure is not lost with it.
                        preview_key = None
                page_row = DocumentPage(
                    id=uuid4(),
                    tenant_id=version.tenant_id,
                    document_version_id=version.id,
                    parse_run_id=run_id,
                    page_number=page.page_number,
                    width=page.width,
                    height=page.height,
                    rotation=page.rotation,
                    source_text_chars=page.source_text_chars,
                    ocr_used=page.page_number in parsed.ocr_pages,
                    ocr_evidence=(
                        "no_source_text_layer" if page.page_number in parsed.ocr_pages else None
                    ),
                    preview_key=preview_key,
                    preview_version_id=preview_version,
                    preview_media_type=preview_type,
                )
                session.add(page_row)
                page_rows[page.page_number] = page_row
            session.flush()

            element_rows: dict[str, DocumentElement] = {}
            for element in parsed.elements:
                owning_page = (
                    page_rows.get(element.page_number) if element.page_number is not None else None
                )
                raw = element.text
                normalized = normalize_text(raw) if raw is not None else None
                element_row = DocumentElement(
                    id=uuid4(),
                    tenant_id=version.tenant_id,
                    document_version_id=version.id,
                    parse_run_id=run_id,
                    page_id=owning_page.id if owning_page else None,
                    page_number=element.page_number,
                    element_type=element.element_type,
                    depth=element.depth,
                    ordinal=element.ordinal,
                    reading_order=element.reading_order,
                    raw_text=raw,
                    normalized_text=normalized,
                    text_normalized=changed(raw, normalized),
                    parser_confidence=element.confidence,
                    source_parser_ref=element.reference,
                    source_label=element.source_label,
                    content_layer=element.content_layer,
                    structure_inferred=False,
                    element_metadata=dict(element.metadata),
                    **self._bbox(element.bbox),
                )
                session.add(element_row)
                element_rows[element.reference] = element_row
            session.flush()

            # Parent links resolve only against elements the parser actually declared as parents.
            for element in parsed.elements:
                if element.parent_reference and element.parent_reference in element_rows:
                    element_rows[element.reference].parent_element_id = element_rows[
                        element.parent_reference
                    ].id

            self._persist_artifacts(session, run_id, version, parsed, page_rows, element_rows)
            self._beat(run)
            session.flush()
        phase_event("PARSE_NORMALIZED", self._correlation(run_id), run_id)

    def _persist_artifacts(
        self,
        session: Session,
        run_id: UUID,
        version: DocumentVersion,
        parsed: ParsedDocument,
        page_rows: dict[int, DocumentPage],
        element_rows: dict[str, DocumentElement],
    ) -> None:
        caption_owner: dict[str, str] = {}
        for element in parsed.elements:
            for reference in element.caption_references:
                caption_owner[reference] = element.reference

        for element in parsed.elements:
            row = element_rows[element.reference]
            page_row = page_rows.get(element.page_number) if element.page_number else None
            caption_element, caption_text = self._caption(element, element_rows)
            common: dict[str, Any] = {
                "tenant_id": version.tenant_id,
                "document_version_id": version.id,
                "parse_run_id": run_id,
                "document_element_id": row.id,
                "page_id": page_row.id if page_row else None,
                "page_number": element.page_number,
                **self._bbox(element.bbox),
            }
            if element.table is not None:
                table = element.table
                cells = [
                    {
                        "text": cell.text,
                        "row": cell.row,
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "column_header": cell.is_column_header,
                        "row_header": cell.is_row_header,
                    }
                    for cell in table.cells
                ]
                session.add(
                    TableArtifact(
                        id=uuid4(),
                        caption_element_id=caption_element,
                        caption_text=caption_text,
                        row_count=table.row_count,
                        column_count=table.column_count,
                        header_row_count=table.header_row_count,
                        cells=cells,
                        markdown=table.markdown,
                        html=table.html,
                        malformed=table.row_count == 0 or table.column_count == 0 or not cells,
                        parser_metadata=dict(table.metadata),
                        **common,
                    )
                )
            if element.figure is not None:
                figure = element.figure
                image_key = image_version = None
                figure_id = uuid4()
                if figure.image:
                    key = figure_key(
                        version.document_id,
                        version.id,
                        run_id,
                        figure_id,
                        self.config.figure_format,
                    )
                    try:
                        stat = self.storage.put_bytes(
                            key, figure.image, figure.media_type or "application/octet-stream"
                        )
                    except Exception:
                        raise ParserError(FIGURE_ARTIFACT_STORAGE_FAILED) from None
                    image_key, image_version = key, stat.version_id
                session.add(
                    FigureArtifact(
                        id=figure_id,
                        caption_element_id=caption_element,
                        caption_text=caption_text,
                        figure_kind=figure.kind,
                        image_key=image_key,
                        image_version_id=image_version,
                        image_media_type=figure.media_type,
                        image_width=figure.width,
                        image_height=figure.height,
                        image_bytes=len(figure.image) if figure.image else None,
                        parser_metadata=dict(figure.metadata),
                        **common,
                    )
                )
            if element.formula is not None:
                formula = element.formula
                session.add(
                    FormulaArtifact(
                        id=uuid4(),
                        source_expression=formula.source_expression,
                        normalized_expression=normalize_expression(formula.source_expression),
                        notation=formula.notation,
                        preceding_element_id=self._neighbour(parsed, element, element_rows, -1),
                        following_element_id=self._neighbour(parsed, element, element_rows, 1),
                        parser_metadata=dict(formula.metadata),
                        **common,
                    )
                )

    # ------------------------------------------------------------------ stage: ENRICHING

    def _enrich_and_validate(
        self, run_id: UUID, parsed: ParsedDocument, source: Path | None = None
    ) -> ParseOutcome:
        with self.sessions.begin() as session:
            run = self._require_run(session, run_id)
            version = session.get(DocumentVersion, run.document_version_id)
            job = session.get(IngestionJob, run.ingestion_job_id) if run.ingestion_job_id else None
            if version is None:
                raise ParserError(PARSER_SOURCE_MISSING)
            if job is None or job.status != Status.NORMALIZING:
                raise ParseCancelled
            transition(session, job, version, Status.ENRICHING, None, service=self.worker_identity)

            pages = list(
                session.scalars(
                    select(DocumentPage)
                    .where(DocumentPage.parse_run_id == run_id)
                    .order_by(DocumentPage.page_number)
                )
            )
            elements = list(
                session.scalars(
                    select(DocumentElement)
                    .where(DocumentElement.parse_run_id == run_id)
                    .order_by(DocumentElement.reading_order)
                )
            )
            tables = list(
                session.scalars(select(TableArtifact).where(TableArtifact.parse_run_id == run_id))
            )
            figures = list(
                session.scalars(select(FigureArtifact).where(FigureArtifact.parse_run_id == run_id))
            )
            formulas = list(
                session.scalars(
                    select(FormulaArtifact).where(FormulaArtifact.parse_run_id == run_id)
                )
            )

            # Deterministic enrichment only: page text rollups, per-page counters and
            # continuation candidates. No inference, no model call, no rewriting of content.
            # A table or formula carries no element text of its own, so the page rollup uses its
            # canonical rendering; otherwise a table-only page would measure as empty.
            table_text = {
                table.document_element_id: table.markdown
                or " ".join(str(cell.get("text", "")) for cell in (table.cells or []))
                for table in tables
            }
            formula_text = {
                formula.document_element_id: formula.normalized_expression for formula in formulas
            }
            by_page: dict[int, list[str]] = {page.page_number: [] for page in pages}
            counts: dict[int, int] = dict.fromkeys(by_page, 0)
            for element in elements:
                if element.page_number in by_page:
                    counts[element.page_number] += 1
                    rendered = (
                        element.normalized_text
                        or table_text.get(element.id)
                        or formula_text.get(element.id)
                    )
                    if rendered:
                        by_page[element.page_number].append(rendered)
            for page in pages:
                page.extracted_text = page_text(by_page[page.page_number])
                page.element_count = counts[page.page_number]
            self._link_table_continuations(tables)

            run.page_count = len(pages)
            run.source_page_count = parsed.source_page_count
            run.element_count = len(elements)
            run.table_count = len(tables)
            run.figure_count = len(figures)
            run.formula_count = len(formulas)
            run.ocr_page_count = sum(1 for page in pages if page.ocr_used)
            self._beat(run)
            session.flush()

            # Only pages the character rule already suspects are re-read from the source, and
            # only to answer one question: is this page's text somewhere in the parse? See ADR-025.
            floor = self.config.thresholds.min_chars_per_page
            suspect = {
                page.page_number
                for page in pages
                if len(page.extracted_text or "") < floor and page.source_text_chars >= floor
            }
            recovered: dict[int, text_recovery.PageRecovery] = {}
            if suspect and source is not None and source.exists():
                recovered = text_recovery.recovery(
                    source, suspect, {p.page_number: (p.extracted_text or "") for p in pages}
                )
            facts = self._facts(parsed, pages, elements, tables, figures, formulas, recovered)
            outcome = validate(facts, self.config.thresholds)
            page_by_number = {page.page_number: page for page in pages}
            element_by_reference = {
                element.source_parser_ref: element
                for element in elements
                if element.source_parser_ref
            }
            for finding in outcome.findings:
                page_row = page_by_number.get(finding.page_number) if finding.page_number else None
                element_row = (
                    element_by_reference.get(finding.element_reference)
                    if finding.element_reference
                    else None
                )
                session.add(
                    ParseValidationFinding(
                        id=uuid4(),
                        tenant_id=version.tenant_id,
                        parse_run_id=run_id,
                        page_id=page_row.id if page_row else None,
                        page_number=finding.page_number,
                        document_element_id=element_row.id if element_row else None,
                        scope=finding.scope,
                        severity=finding.severity,
                        code=finding.code,
                        message=finding.message,
                        details=dict(finding.details),
                    )
                )
                self.metrics.findings.labels(severity=finding.severity.value).inc()

            run.validation_result = outcome.result
            target = RESULT_STATUS[outcome.result]
            now = datetime.now(UTC)
            run.completed_at = now
            if target == Status.READY_FOR_CHUNKING:
                run.status = ParseRunStatus.SUCCEEDED
                # Supersede any previously active run only now that this one is complete.
                session.execute(
                    update(ParseRun)
                    .where(
                        ParseRun.document_version_id == version.id,
                        ParseRun.id != run_id,
                        ParseRun.is_active.is_(True),
                    )
                    .values(is_active=False)
                )
                session.flush()
                run.is_active = True
                version.page_count = run.page_count
                transition(session, job, version, target, None, service=self.worker_identity)
                self.metrics.succeeded.inc()
            else:
                run.status = ParseRunStatus.FAILED
                run.error_code = (
                    "PARSE_NEEDS_REVIEW"
                    if target == Status.NEEDS_REVIEW
                    else "PARSE_VALIDATION_FAILED"
                )
                run.error_message = safe_message(run.error_code)
                transition(
                    session,
                    job,
                    version,
                    target,
                    None,
                    service=self.worker_identity,
                    error_code=run.error_code,
                    error_message=run.error_message,
                )
                if target == Status.NEEDS_REVIEW:
                    self.metrics.needs_review.inc()
                else:
                    self.metrics.failed.labels(code="PARSE_VALIDATION_FAILED").inc()
            self.metrics.pages.inc(run.page_count or 0)
            self.metrics.tables.inc(run.table_count or 0)
            self.metrics.figures.inc(run.figure_count or 0)
            self.metrics.formulas.inc(run.formula_count or 0)
            self.metrics.ocr_pages.inc(run.ocr_page_count or 0)
            audit(
                session,
                version.tenant_id,
                None,
                "PARSE_RUN_COMPLETED",
                run_id,
                run.correlation_id,
                {
                    "result": outcome.result.value,
                    "status": target.value,
                    "pages": run.page_count,
                    "elements": run.element_count,
                    "tables": run.table_count,
                    "figures": run.figure_count,
                    "formulas": run.formula_count,
                    "findings": len(outcome.findings),
                },
            )
            phase_event("PARSE_RUN_COMPLETED", run.correlation_id, run_id)
            return ParseOutcome(run_id, target, outcome.result)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _bbox(box: Any) -> dict[str, Any]:
        if box is None:
            return {
                "bbox_x1": None,
                "bbox_y1": None,
                "bbox_x2": None,
                "bbox_y2": None,
                "bbox_origin": None,
            }
        return {
            "bbox_x1": box.x1,
            "bbox_y1": box.y1,
            "bbox_x2": box.x2,
            "bbox_y2": box.y2,
            "bbox_origin": box.origin,
        }

    @staticmethod
    def _caption(
        element: ParsedElement, element_rows: dict[str, DocumentElement]
    ) -> tuple[UUID | None, str | None]:
        """Explicit parser-declared caption relation only; never nearest-neighbour geometry."""
        for reference in element.caption_references:
            row = element_rows.get(reference)
            if row is not None:
                return row.id, row.normalized_text
        return None, None

    @staticmethod
    def _neighbour(
        parsed: ParsedDocument,
        element: ParsedElement,
        element_rows: dict[str, DocumentElement],
        offset: int,
    ) -> UUID | None:
        """Adjacent explanatory prose in reading order, on the same page.

        Only a paragraph counts. A caption belongs to the figure or table it describes, and a
        heading is not an explanation, so neither is presented as related to the formula.
        """
        index = element.reading_order + offset
        for candidate in parsed.elements:
            if candidate.reading_order != index:
                continue
            if candidate.element_type is not ElementType.PARAGRAPH:
                return None
            if candidate.page_number != element.page_number:
                return None
            row = element_rows.get(candidate.reference)
            return row.id if row else None
        return None

    @staticmethod
    def _link_table_continuations(tables: list[TableArtifact]) -> None:
        """Flag a possible continuation without merging anything.

        The only evidence accepted is: consecutive pages, identical column count, and the later
        table declaring no header row. Uncertain pairs stay separate tables, always.
        """
        ordered = sorted(
            (table for table in tables if table.page_number is not None),
            key=lambda table: (table.page_number or 0, table.created_at),
        )
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if (previous.page_number or 0) + 1 != (current.page_number or 0):
                continue
            if previous.column_count == 0 or previous.column_count != current.column_count:
                continue
            if current.header_row_count != 0 or previous.header_row_count == 0:
                continue
            current.possible_continuation = True
            current.continuation_of_id = previous.id
            current.continuation_evidence = "adjacent_page_same_columns"
            current.table_group_id = previous.table_group_id or previous.id
            previous.table_group_id = current.table_group_id

    def _facts(
        self,
        parsed: ParsedDocument,
        pages: list[DocumentPage],
        elements: list[DocumentElement],
        tables: list[TableArtifact],
        figures: list[FigureArtifact],
        formulas: list[FormulaArtifact],
        recovered: dict[int, text_recovery.PageRecovery] | None = None,
    ) -> ParseFacts:
        recovered = recovered or {}
        page_size = {page.id: (page.width, page.height) for page in pages}
        element_facts = []
        for element in elements:
            has_bbox = element.bbox_x1 is not None
            valid = bool(
                has_bbox
                and element.bbox_x2 is not None
                and element.bbox_y2 is not None
                and element.bbox_x1 is not None
                and element.bbox_y1 is not None
                and element.bbox_x2 > element.bbox_x1
                and element.bbox_y2 > element.bbox_y1
                and element.bbox_x1 >= 0
                and element.bbox_y1 >= 0
            )
            width, height = (
                page_size.get(element.page_id, (0.0, 0.0)) if element.page_id else (0.0, 0.0)
            )
            in_page = bool(
                valid
                and element.bbox_x2 is not None
                and element.bbox_y2 is not None
                and element.bbox_x2 <= width + 2
                and element.bbox_y2 <= height + 2
            )
            element_facts.append(
                ElementFacts(
                    reference=element.source_parser_ref or str(element.id),
                    element_type=element.element_type,
                    reading_order=element.reading_order,
                    page_number=element.page_number,
                    has_bbox=has_bbox,
                    bbox_valid=valid,
                    bbox_in_page=in_page,
                    # Structural containers (a list, a section) are parser groups with no
                    # geometry of their own; their absent page is expected, not a defect.
                    is_container="group" in (element.element_metadata or {}),
                )
            )
        element_by_id = {element.id: element for element in elements}

        def reference(element_id: UUID) -> str:
            found = element_by_id.get(element_id)
            return (found.source_parser_ref if found else None) or str(element_id)

        return ParseFacts(
            source_page_count=parsed.source_page_count,
            previews_requested=self.config.generate_page_previews,
            figures_requested=self.config.extract_figures,
            parser_warnings=parsed.warnings,
            pages=tuple(
                PageFacts(
                    page_number=page.page_number,
                    width=page.width,
                    height=page.height,
                    text_chars=len(page.extracted_text or ""),
                    element_count=page.element_count,
                    source_text_chars=page.source_text_chars,
                    ocr_used=page.ocr_used,
                    has_preview=page.preview_key is not None,
                    source_text_recovery=recovered.get(page.page_number),
                )
                for page in pages
            ),
            elements=tuple(element_facts),
            tables=tuple(
                TableFacts(
                    reference=reference(table.document_element_id),
                    page_number=table.page_number,
                    row_count=table.row_count,
                    column_count=table.column_count,
                    cell_count=len(table.cells or []),
                    max_row_index=max((cell["row"] for cell in table.cells or []), default=-1),
                    max_column_index=max(
                        (cell["column"] for cell in table.cells or []), default=-1
                    ),
                    caption_resolved=table.caption_element_id is not None,
                    caption_referenced=table.caption_element_id is not None
                    or bool(table.caption_text),
                )
                for table in tables
            ),
            figures=tuple(
                FigureFacts(
                    reference=reference(figure.document_element_id),
                    page_number=figure.page_number,
                    has_image=figure.image_key is not None,
                    caption_resolved=figure.caption_element_id is not None,
                    caption_referenced=figure.caption_element_id is not None
                    or bool(figure.caption_text),
                )
                for figure in figures
            ),
            formulas=tuple(
                FormulaFacts(
                    reference=reference(formula.document_element_id),
                    page_number=formula.page_number,
                    has_expression=bool((formula.normalized_expression or "").strip()),
                )
                for formula in formulas
            ),
        )

    def _require_run(self, session: Session, run_id: UUID) -> ParseRun:
        run = session.get(ParseRun, run_id)
        if run is None:
            raise ParserError(PARSE_PERSISTENCE_FAILED, "run_missing")
        return run

    def _beat(self, run: ParseRun) -> None:
        now = datetime.now(UTC)
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=self.config.lease_seconds)

    def _correlation(self, run_id: UUID) -> UUID:
        with self.sessions() as session:
            run = session.get(ParseRun, run_id)
            return run.correlation_id if run else uuid4()

    def _advance_to_ready(
        self,
        session: Session,
        job: IngestionJob,
        version: DocumentVersion,
        run: ParseRun,
    ) -> None:
        """Idempotent replay: an identical parse already exists, so only the job moves."""
        for stage in (Status.PARSING, Status.NORMALIZING, Status.ENRICHING):
            transition(session, job, version, stage, None, service=self.worker_identity)
        version.page_count = run.page_count
        transition(
            session, job, version, Status.READY_FOR_CHUNKING, None, service=self.worker_identity
        )
        audit(
            session,
            version.tenant_id,
            None,
            "PARSE_RUN_REUSED",
            run.id,
            job.correlation_id,
            {"parse_run_id": str(run.id), "reason": "identical_parser_config_and_source"},
        )

    # ------------------------------------------------------------------ terminal paths

    def _fail(self, run_id: UUID, code: str, message: str) -> None:
        with self.sessions.begin() as session:
            run = session.get(ParseRun, run_id)
            if run is None:
                return
            run.status = ParseRunStatus.FAILED
            run.is_active = False
            run.error_code = code
            run.error_message = message[:300]
            run.completed_at = datetime.now(UTC)
            job = session.get(IngestionJob, run.ingestion_job_id) if run.ingestion_job_id else None
            version = session.get(DocumentVersion, run.document_version_id)
            if job is None or version is None:
                return
            if job.status in {Status.PARSING, Status.NORMALIZING, Status.ENRICHING}:
                transition(
                    session,
                    job,
                    version,
                    Status.FAILED,
                    None,
                    service=self.worker_identity,
                    error_code=code,
                    error_message=message[:300],
                )
            audit(
                session,
                run.tenant_id,
                None,
                "PARSE_RUN_FAILED",
                run_id,
                run.correlation_id,
                {"code": code},
            )
        phase_event("PARSE_RUN_FAILED", self._correlation(run_id), run_id)

    def _release_cancelled(self, run_id: UUID) -> None:
        with self.sessions.begin() as session:
            run = session.get(ParseRun, run_id)
            if run is None:
                return
            run.status = ParseRunStatus.CANCELLED
            run.is_active = False
            run.completed_at = datetime.now(UTC)
            run.error_code = "PARSE_CANCELLED"
            run.error_message = "The ingestion job left the parse path before completion."


def reap_expired_leases(
    sessions: sessionmaker[Session], config: ParsingConfig, service_identity: str = "dispatcher"
) -> int:
    """Release parse runs whose worker stopped heartbeating.

    Without this a crashed worker would leave a job stuck in PARSING forever with no operator
    path forward. The job becomes FAILED with a retryable code, so the existing bounded retry
    can start a fresh ParseRun.
    """
    released = 0
    from app.ingestion.parser.errors import PARSER_LEASE_EXPIRED

    with sessions.begin() as session:
        runs = list(
            session.scalars(
                select(ParseRun)
                .where(
                    ParseRun.status == ParseRunStatus.RUNNING,
                    ParseRun.lease_expires_at < datetime.now(UTC),
                )
                .with_for_update(skip_locked=True)
                .limit(20)
            )
        )
        for run in runs:
            run.status = ParseRunStatus.FAILED
            run.is_active = False
            run.error_code = PARSER_LEASE_EXPIRED
            run.error_message = safe_message(PARSER_LEASE_EXPIRED)
            run.completed_at = datetime.now(UTC)
            job = session.get(IngestionJob, run.ingestion_job_id) if run.ingestion_job_id else None
            version = session.get(DocumentVersion, run.document_version_id)
            if (
                job is not None
                and version is not None
                and job.status
                in {
                    Status.PARSING,
                    Status.NORMALIZING,
                    Status.ENRICHING,
                }
            ):
                transition(
                    session,
                    job,
                    version,
                    Status.FAILED,
                    None,
                    service=service_identity,
                    error_code=PARSER_LEASE_EXPIRED,
                    error_message=safe_message(PARSER_LEASE_EXPIRED),
                )
            released += 1
    return released
