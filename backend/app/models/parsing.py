"""M2 parse provenance.

Page numbers are 1-based, matching the user-visible printed order supplied by the parser.
Bounding boxes are stored in PDF points with a TOPLEFT origin (x1,y1 = upper-left corner,
x2,y2 = lower-right), converted once at normalization from the coordinate system the parser
reports, and always paired with the owning page width/height. No 0-based page index is persisted.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDTimestampMixin
from app.models.documents import enum_type
from app.models.enums import (
    CoordinateOrigin,
    ElementType,
    OcrMode,
    ParseResult,
    ParseRunStatus,
    Severity,
)


class BoundingBoxMixin:
    """Nullable box; an element without parser-supplied geometry stays explicitly unlocated."""

    bbox_x1: Mapped[float | None] = mapped_column(Float)
    bbox_y1: Mapped[float | None] = mapped_column(Float)
    bbox_x2: Mapped[float | None] = mapped_column(Float)
    bbox_y2: Mapped[float | None] = mapped_column(Float)
    bbox_origin: Mapped[CoordinateOrigin | None] = mapped_column(
        enum_type(CoordinateOrigin, "coordinate_origin")
    )


class ParseRun(UUIDTimestampMixin, Base):
    """One durable, reproducible parse attempt. Previous successful runs are never overwritten."""

    __tablename__ = "parse_runs"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint(
            "id", "document_version_id", "tenant_id", name="uq_parse_runs_id_version_tenant"
        ),
        # Exactly one active run per version; a superseded run keeps its rows for comparison.
        Index(
            "uq_parse_runs_active_version",
            "document_version_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_parse_runs_document_version_id", "document_version_id"),
        # A flagged parse can become the active dataset only through a recorded curator
        # decision, which is what REVIEWED_ACCEPTED means; see ParseReviewDecision.
        CheckConstraint(
            "NOT is_active OR status IN ('SUCCEEDED', 'REVIEWED_ACCEPTED')",
            name="active_run_usable",
        ),
        CheckConstraint("attempt > 0", name="positive_attempt"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    ingestion_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("ingestion_jobs.id"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    parser_name: Mapped[str] = mapped_column(String(60))
    parser_provider: Mapped[str] = mapped_column(String(60))
    parser_version: Mapped[str] = mapped_column(String(60))
    configuration_version: Mapped[str] = mapped_column(String(80))
    configuration_fingerprint: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_checksum: Mapped[str] = mapped_column(String(64))
    source_object_version_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[ParseRunStatus] = mapped_column(
        enum_type(ParseRunStatus, "parse_run_status"), index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    raw_artifact_key: Mapped[str | None] = mapped_column(String(600))
    raw_artifact_version_id: Mapped[str | None] = mapped_column(String(200))
    raw_artifact_bytes: Mapped[int | None] = mapped_column(Integer)
    ocr_mode: Mapped[OcrMode] = mapped_column(enum_type(OcrMode, "ocr_mode"))
    ocr_engine: Mapped[str | None] = mapped_column(String(60))
    tables_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    formulas_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    figures_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    previews_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_result: Mapped[ParseResult | None] = mapped_column(
        enum_type(ParseResult, "parse_result")
    )
    page_count: Mapped[int | None]
    source_page_count: Mapped[int | None]
    element_count: Mapped[int | None]
    table_count: Mapped[int | None]
    figure_count: Mapped[int | None]
    formula_count: Mapped[int | None]
    ocr_page_count: Mapped[int | None]
    duration_ms: Mapped[int | None]
    worker_identity: Mapped[str | None] = mapped_column(String(120))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID]
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(300))


class DocumentPage(BoundingBoxMixin, UUIDTimestampMixin, Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("parse_run_id", "page_number"),
        UniqueConstraint("id", "parse_run_id"),
        CheckConstraint("page_number >= 1", name="page_number_is_one_based"),
        CheckConstraint("width > 0 AND height > 0", name="positive_page_size"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("parse_runs.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int]
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    element_count: Mapped[int] = mapped_column(Integer, default=0)
    source_text_chars: Mapped[int] = mapped_column(Integer, default=0)
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False)
    ocr_evidence: Mapped[str | None] = mapped_column(String(60))
    preview_key: Mapped[str | None] = mapped_column(String(600))
    preview_version_id: Mapped[str | None] = mapped_column(String(200))
    preview_media_type: Mapped[str | None] = mapped_column(String(60))


class DocumentElement(BoundingBoxMixin, UUIDTimestampMixin, Base):
    """Parser-independent structural element.

    `ordinal` is the position among siblings; `reading_order` is the document-wide deterministic
    traversal position. Structure the parser does not confidently expose stays NULL.
    """

    __tablename__ = "document_elements"
    __table_args__ = (
        UniqueConstraint("parse_run_id", "reading_order"),
        UniqueConstraint("id", "parse_run_id"),
        Index("ix_document_elements_run_page", "parse_run_id", "page_id"),
        Index("ix_document_elements_run_type", "parse_run_id", "element_type"),
        CheckConstraint("reading_order >= 0 AND ordinal >= 0", name="non_negative_order"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("parse_runs.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE")
    )
    page_number: Mapped[int | None]
    parent_element_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_elements.id", ondelete="CASCADE")
    )
    element_type: Mapped[ElementType] = mapped_column(enum_type(ElementType, "element_type"))
    depth: Mapped[int] = mapped_column(Integer, default=0)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    reading_order: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    text_normalized: Mapped[bool] = mapped_column(Boolean, default=False)
    parser_confidence: Mapped[float | None] = mapped_column(Float)
    source_parser_ref: Mapped[str | None] = mapped_column(String(120))
    source_label: Mapped[str | None] = mapped_column(String(60))
    content_layer: Mapped[str | None] = mapped_column(String(30))
    structure_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    element_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class TableArtifact(BoundingBoxMixin, UUIDTimestampMixin, Base):
    """Canonical structured table. `cells` is primary; markdown/html are convenience renderings."""

    __tablename__ = "table_artifacts"
    __table_args__ = (
        UniqueConstraint("document_element_id"),
        UniqueConstraint("id", "parse_run_id"),
        CheckConstraint("row_count >= 0 AND column_count >= 0", name="non_negative_table_size"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("parse_runs.id", ondelete="CASCADE"), index=True
    )
    document_element_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_elements.id", ondelete="CASCADE")
    )
    page_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE")
    )
    page_number: Mapped[int | None]
    caption_element_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_elements.id", ondelete="SET NULL")
    )
    caption_text: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)
    header_row_count: Mapped[int] = mapped_column(Integer, default=0)
    cells: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    markdown: Mapped[str | None] = mapped_column(Text)
    html: Mapped[str | None] = mapped_column(Text)
    # Continuation is recorded only when a deterministic rule justifies it; never a merge.
    table_group_id: Mapped[UUID | None]
    continuation_of_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("table_artifacts.id", ondelete="SET NULL")
    )
    possible_continuation: Mapped[bool] = mapped_column(Boolean, default=False)
    continuation_evidence: Mapped[str | None] = mapped_column(String(60))
    malformed: Mapped[bool] = mapped_column(Boolean, default=False)
    parser_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class FigureArtifact(BoundingBoxMixin, UUIDTimestampMixin, Base):
    __tablename__ = "figure_artifacts"
    __table_args__ = (
        UniqueConstraint("document_element_id"),
        UniqueConstraint("id", "parse_run_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("parse_runs.id", ondelete="CASCADE"), index=True
    )
    document_element_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_elements.id", ondelete="CASCADE")
    )
    page_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE")
    )
    page_number: Mapped[int | None]
    caption_element_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_elements.id", ondelete="SET NULL")
    )
    caption_text: Mapped[str | None] = mapped_column(Text)
    figure_kind: Mapped[str | None] = mapped_column(String(40))
    image_key: Mapped[str | None] = mapped_column(String(600))
    image_version_id: Mapped[str | None] = mapped_column(String(200))
    image_media_type: Mapped[str | None] = mapped_column(String(60))
    image_width: Mapped[int | None]
    image_height: Mapped[int | None]
    image_bytes: Mapped[int | None]
    parser_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class FormulaArtifact(BoundingBoxMixin, UUIDTimestampMixin, Base):
    """Explicit formula record.

    `normalized_expression` is populated only by deterministic normalization; no model is ever
    asked to reinterpret, complete or correct an expression.
    """

    __tablename__ = "formula_artifacts"
    __table_args__ = (
        UniqueConstraint("document_element_id"),
        UniqueConstraint("id", "parse_run_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    document_version_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("parse_runs.id", ondelete="CASCADE"), index=True
    )
    document_element_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_elements.id", ondelete="CASCADE")
    )
    page_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE")
    )
    page_number: Mapped[int | None]
    source_expression: Mapped[str | None] = mapped_column(Text)
    normalized_expression: Mapped[str | None] = mapped_column(Text)
    notation: Mapped[str | None] = mapped_column(String(40))
    preceding_element_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_elements.id", ondelete="SET NULL")
    )
    following_element_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_elements.id", ondelete="SET NULL")
    )
    parser_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ParseValidationFinding(UUIDTimestampMixin, Base):
    """Append-only quality finding. Messages and details are operator-safe, never source text."""

    __tablename__ = "parse_validation_findings"
    __table_args__ = (
        Index("ix_parse_validation_findings_run_severity", "parse_run_id", "severity"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("parse_runs.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE")
    )
    page_number: Mapped[int | None]
    document_element_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_elements.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(String(20))
    severity: Mapped[Severity] = mapped_column(enum_type(Severity, "finding_severity"))
    code: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ParseReviewDecision(UUIDTimestampMixin, Base):
    """An authorized curator's explicit decision about one exact flagged parse run.

    The automated verdict is never rewritten. `parse_runs.validation_result` stays NEEDS_REVIEW
    and every `ParseValidationFinding` stays exactly as the quality layer wrote it; acceptance is
    recorded here instead, as a separate append-only fact with its own reviewer, rationale and
    correlation identity.

    The decision is bound to one parse run by foreign key, so it cannot follow a reparse: a new
    run is a new row with no decision of its own. `findings_digest` pins what was actually
    reviewed — the append-only trigger blocks UPDATE and DELETE but not INSERT, so without a
    digest a finding appended later would silently appear to have been covered by the decision.
    """

    __tablename__ = "parse_review_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parse_run_id", "tenant_id"],
            ["parse_runs.id", "parse_runs.tenant_id"],
            name="fk_parse_review_decisions_parse_run",
            ondelete="CASCADE",
        ),
        # One acceptance per run, ever. This is the idempotency fence: a concurrent second
        # ACCEPT cannot create a second decision even if two curators race.
        Index(
            "uq_parse_review_decisions_accept",
            "parse_run_id",
            unique=True,
            postgresql_where=text("decision = 'ACCEPT'"),
        ),
        Index("ix_parse_review_decisions_parse_run_id", "parse_run_id"),
        CheckConstraint("decision IN ('ACCEPT')", name="decision_vocabulary"),
        CheckConstraint(
            "validation_result_at_decision = 'NEEDS_REVIEW'", name="reviewed_result_recorded"
        ),
        CheckConstraint("finding_count >= 0", name="finding_count_not_negative"),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    parse_run_id: Mapped[UUID] = mapped_column()
    decision: Mapped[str] = mapped_column(String(6))
    reviewer_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    #: Curator-authored prose. Stored here and nowhere else: it may quote document content, so it
    #: never reaches audit metadata or the structured log.
    rationale: Mapped[str] = mapped_column(String(2000))
    #: What was accepted, copied at decision time so the record stands alone.
    validation_result_at_decision: Mapped[str] = mapped_column(String(18))
    finding_count: Mapped[int] = mapped_column(Integer)
    findings_digest: Mapped[str] = mapped_column(String(64))
    configuration_fingerprint: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[UUID] = mapped_column()
