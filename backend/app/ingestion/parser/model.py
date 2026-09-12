"""Parser-independent normalized representation.

Nothing outside `app.ingestion.parser.docling_adapter` may import Docling. The domain
consumes only the frozen dataclasses declared here, so a different parser can be substituted
without touching persistence, validation, APIs or the UI.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any, Protocol

from app.models.enums import CoordinateOrigin, ElementType


def document_budget_seconds(
    pages: int, call_seconds: int, seconds_per_page: float, ceiling: int
) -> int:
    """Time budget for a whole document, or 0 when no whole-document budget applies.

    One conversion call plus a share for every page, capped. A constant cannot be right for
    a corpus holding both a one-page leaflet and a 932-page textbook: the value that lets
    the book finish would let a stuck one-page parse hold the worker for hours.

    Declared here, in the parser-independent layer, so the policy in `ParsingConfig` and the
    enforcement in the adapter cannot drift apart into two different formulas.
    """
    if seconds_per_page <= 0 or ceiling <= 0:
        return 0
    return min(ceiling, call_seconds + ceil(pages * seconds_per_page))


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
    #: Pages per conversion call for a long document; 0 converts the whole document at once.
    page_window_size: int = 0
    #: Documents at or below this page count always use the single-call path.
    page_window_threshold: int = 25
    #: Inputs to the whole-document budget, which bounds the parse across every window as
    #: opposed to `timeout_seconds` which bounds one conversion call. The budget scales with
    #: length because no constant fits both a leaflet and a 900-page textbook. Either value
    #: at 0 leaves the document bounded per call only.
    document_seconds_per_page: float = 0.0
    max_document_timeout_seconds: int = 0


class DocumentParser(Protocol):
    name: str

    def parse(
        self,
        source: ParseSource,
        config: ParserConfig,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ParsedDocument:
        """Convert a source document.

        `on_progress(pages_done, pages_total)` lets a long conversion report advancement so
        the caller can renew its lease. An implementation that converts in one pass may
        ignore it; one that converts incrementally should call it between units of work.
        """
        ...


def page_index(pages: Sequence[ParsedPage]) -> dict[int, ParsedPage]:
    return {page.page_number: page for page in pages}
