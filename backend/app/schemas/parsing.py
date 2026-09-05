"""Inspection views for parsed material.

Views expose provenance (page, coordinates, parser reference) so a future citation viewer can
resolve an exact source region. They never expose object-storage keys, endpoints or credentials:
binary artifacts are reachable only through authorized streaming endpoints on this API.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import (
    CoordinateOrigin,
    ElementType,
    OcrMode,
    ParseResult,
    ParseRunStatus,
    Severity,
)
from app.schemas.documents import ORMView


class BoundingBoxView(BaseModel):
    """PDF points, TOPLEFT origin, relative to the page width/height on the owning page view."""

    x1: float
    y1: float
    x2: float
    y2: float
    origin: CoordinateOrigin


def box(model: Any) -> BoundingBoxView | None:
    if model.bbox_x1 is None or model.bbox_origin is None:
        return None
    return BoundingBoxView(
        x1=model.bbox_x1,
        y1=model.bbox_y1,
        x2=model.bbox_x2,
        y2=model.bbox_y2,
        origin=model.bbox_origin,
    )


class ParseRunView(ORMView):
    id: UUID
    document_version_id: UUID
    ingestion_job_id: UUID | None
    attempt: int
    parser_name: str
    parser_provider: str
    parser_version: str
    configuration_version: str
    configuration_fingerprint: str
    status: ParseRunStatus
    is_active: bool
    validation_result: ParseResult | None
    ocr_mode: OcrMode
    ocr_engine: str | None
    tables_enabled: bool
    formulas_enabled: bool
    figures_enabled: bool
    previews_enabled: bool
    page_count: int | None
    source_page_count: int | None
    element_count: int | None
    table_count: int | None
    figure_count: int | None
    formula_count: int | None
    ocr_page_count: int | None
    raw_artifact_bytes: int | None
    duration_ms: int | None
    started_at: datetime | None
    completed_at: datetime | None
    correlation_id: UUID
    error_code: str | None
    error_message: str | None
    created_at: datetime
    finding_counts: dict[str, int] = {}


class PageView(ORMView):
    id: UUID
    parse_run_id: UUID
    page_number: int
    width: float
    height: float
    rotation: int
    element_count: int
    source_text_chars: int
    ocr_used: bool
    ocr_evidence: str | None
    has_preview: bool = False
    preview_media_type: str | None


class PageDetailView(PageView):
    extracted_text: str


class ElementView(ORMView):
    id: UUID
    parse_run_id: UUID
    page_id: UUID | None
    page_number: int | None
    parent_element_id: UUID | None
    element_type: ElementType
    depth: int
    ordinal: int
    reading_order: int
    raw_text: str | None
    normalized_text: str | None
    text_normalized: bool
    parser_confidence: float | None
    source_parser_ref: str | None
    source_label: str | None
    content_layer: str | None
    structure_inferred: bool
    bbox: BoundingBoxView | None = None


class TableCellView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    row: int = 0
    column: int = 0
    row_span: int = 1
    column_span: int = 1
    column_header: bool = False
    row_header: bool = False


class TableView(ORMView):
    id: UUID
    parse_run_id: UUID
    document_element_id: UUID
    page_id: UUID | None
    page_number: int | None
    caption_element_id: UUID | None
    caption_text: str | None
    row_count: int
    column_count: int
    header_row_count: int
    table_group_id: UUID | None
    continuation_of_id: UUID | None
    possible_continuation: bool
    continuation_evidence: str | None
    malformed: bool
    bbox: BoundingBoxView | None = None


class TableDetailView(TableView):
    cells: list[TableCellView] = []
    markdown: str | None
    html: str | None


class FigureView(ORMView):
    id: UUID
    parse_run_id: UUID
    document_element_id: UUID
    page_id: UUID | None
    page_number: int | None
    caption_element_id: UUID | None
    caption_text: str | None
    figure_kind: str | None
    image_media_type: str | None
    image_width: int | None
    image_height: int | None
    image_bytes: int | None
    has_image: bool = False
    bbox: BoundingBoxView | None = None


class FormulaView(ORMView):
    id: UUID
    parse_run_id: UUID
    document_element_id: UUID
    page_id: UUID | None
    page_number: int | None
    source_expression: str | None
    normalized_expression: str | None
    notation: str | None
    preceding_element_id: UUID | None
    following_element_id: UUID | None
    bbox: BoundingBoxView | None = None


class FindingView(ORMView):
    id: UUID
    parse_run_id: UUID
    page_id: UUID | None
    page_number: int | None
    document_element_id: UUID | None
    scope: str
    severity: Severity
    code: str
    message: str
    details: dict[str, Any]
    created_at: datetime


class ParseSummaryView(BaseModel):
    """Compact, honest status for the document details screen."""

    document_version_id: UUID
    ingestion_status: str
    parse_run: ParseRunView | None = None
    parse_runs: int = 0
