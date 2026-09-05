"""Parser-independent normalized representation.

Nothing outside `app.ingestion.parser.docling_adapter` may import Docling. The domain
consumes only the frozen dataclasses declared here, so a different parser can be substituted
without touching persistence, validation, APIs or the UI.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.models.enums import CoordinateOrigin, ElementType


@dataclass(frozen=True)
class BoundingBox:
    """PDF points, TOPLEFT origin: (x1, y1) upper-left, (x2, y2) lower-right of the page image.

    Rotation is already applied by the parser backend, so the box is expressed against the
    upright page whose width/height are recorded on the owning `ParsedPage`.
    """

    x1: float
    y1: float
    x2: float
    y2: float
    origin: CoordinateOrigin = CoordinateOrigin.TOPLEFT

    @property
    def valid(self) -> bool:
        return self.x2 > self.x1 and self.y2 > self.y1 and self.x1 >= 0 and self.y1 >= 0

    def within(self, width: float, height: float, tolerance: float = 2.0) -> bool:
        return self.valid and self.x2 <= width + tolerance and self.y2 <= height + tolerance


@dataclass(frozen=True)
class ParsedPage:
    page_number: int  # 1-based, user-facing.
    width: float
    height: float
    rotation: int = 0
    # Characters recovered from the source text layer alone, before any OCR. Zero on a scan.
    source_text_chars: int = 0
    preview_image: bytes | None = None
    preview_media_type: str | None = None


@dataclass(frozen=True)
class ParsedTableCell:
    text: str
    row: int  # 0-based grid coordinates inside the table only, never a page number.
    column: int
    row_span: int = 1
    column_span: int = 1
    is_column_header: bool = False
    is_row_header: bool = False
    bbox: BoundingBox | None = None


@dataclass(frozen=True)
class ParsedTable:
    row_count: int
    column_count: int
    header_row_count: int
    cells: tuple[ParsedTableCell, ...]
    markdown: str | None = None
    html: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedFigure:
    image: bytes | None
    media_type: str | None
    width: int | None
    height: int | None
    kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedFormula:
    source_expression: str | None
    notation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedElement:
    """One structural element in deterministic reading order.

    `reference` is the parser's own stable identifier for this node; `parent_reference` is set
    only when the parser exposes real containment. Absent structure stays ``None`` rather than
    being reconstructed from geometry.
    """

    reference: str
    element_type: ElementType
    reading_order: int
    ordinal: int
    depth: int
    parent_reference: str | None = None
    page_number: int | None = None
    text: str | None = None
    bbox: BoundingBox | None = None
    confidence: float | None = None
    source_label: str | None = None
    content_layer: str | None = None
    caption_of: str | None = None
    caption_references: tuple[str, ...] = ()
    table: ParsedTable | None = None
    figure: ParsedFigure | None = None
    formula: ParsedFormula | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedDocument:
    parser_name: str
    parser_provider: str
    parser_version: str
    pages: tuple[ParsedPage, ...]
    elements: tuple[ParsedElement, ...]
    raw_artifact: bytes
    raw_artifact_media_type: str = "application/json"
    # Page count read from the source container itself, for reconciliation against `pages`.
    source_page_count: int | None = None
    ocr_engine: str | None = None
    ocr_pages: frozenset[int] = frozenset()
    warnings: tuple[str, ...] = ()
    duration_ms: int = 0


@dataclass(frozen=True)
class ParseSource:
    """A local, already-validated copy of the original file plus its pinned identity."""

    path: Path
    sha256: str
    filename: str
    media_type: str = "application/pdf"


@dataclass(frozen=True)
class ParserConfig:
    """Parser-independent request options, projected from the frozen ParsingConfig policy."""

    ocr_mode: str
    extract_tables: bool
    extract_formulas: bool
    extract_figures: bool
    generate_page_previews: bool
    preview_scale: float
    timeout_seconds: int
    max_pages: int
    preview_format: str = "webp"
    figure_format: str = "png"
    # Pinned so the same document produces the same layout prediction on any host.
    threads: int = 4
    temp_dir: Path | None = None


class DocumentParser(Protocol):
    name: str

    def parse(self, source: ParseSource, config: ParserConfig) -> ParsedDocument: ...


def page_index(pages: Sequence[ParsedPage]) -> dict[int, ParsedPage]:
    return {page.page_number: page for page in pages}
