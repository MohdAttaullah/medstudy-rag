"""Docling adapter.

This module is the only place in the application that imports Docling. It converts a validated
local PDF into the parser-independent `ParsedDocument` and never mutates the source document,
rewrites text, or asks any model to interpret content it could not read.

Pinned behaviour of the installed version is recorded in docs/architecture/document-parsing.md.
"""

from __future__ import annotations

import ctypes
import gc
import importlib.metadata as metadata
import io
import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.ingestion.parser.errors import (
    OCR_FAILED,
    PARSER_INTERNAL_ERROR,
    PARSER_OOM,
    PARSER_PAGE_LIMIT_EXCEEDED,
    PARSER_SOURCE_CORRUPT,
    PARSER_SOURCE_MISSING,
    PARSER_TIMEOUT,
    PARSER_UNAVAILABLE,
    PARSER_UNSUPPORTED_PDF,
    ParserError,
)
from app.ingestion.parser.model import (
    BoundingBox,
    ParsedDocument,
    ParsedElement,
    ParsedFigure,
    ParsedFormula,
    ParsedPage,
    ParsedTable,
    ParsedTableCell,
    ParserConfig,
    ParseSource,
    document_budget_seconds,
)
from app.models.enums import CoordinateOrigin, ElementType

logger = logging.getLogger("medical_rag.requests")

# Docling item labels mapped onto the parser-independent element vocabulary. Anything the parser
# emits that is not listed here becomes OTHER rather than being silently reclassified.
LABEL_TYPES: dict[str, ElementType] = {
    "title": ElementType.TITLE,
    "section_header": ElementType.HEADING,
    "text": ElementType.PARAGRAPH,
    "paragraph": ElementType.PARAGRAPH,
    "reference": ElementType.PARAGRAPH,
    "list_item": ElementType.LIST_ITEM,
    "table": ElementType.TABLE,
    "formula": ElementType.FORMULA,
    "picture": ElementType.FIGURE,
    "chart": ElementType.FIGURE,
    "caption": ElementType.CAPTION,
    "footnote": ElementType.FOOTNOTE,
    "page_header": ElementType.PAGE_HEADER,
    "page_footer": ElementType.PAGE_FOOTER,
    "code": ElementType.CODE,
    "document_index": ElementType.OTHER,
}
GROUP_TYPES: dict[str, ElementType] = {
    "list": ElementType.LIST,
    "ordered_list": ElementType.LIST,
    "chapter": ElementType.SECTION,
    "section": ElementType.SECTION,
    "inline": ElementType.OTHER,
    "picture_area": ElementType.OTHER,
    "key_value_area": ElementType.OTHER,
    "form_area": ElementType.OTHER,
}
# A page whose text layer yields fewer characters than this, on a run where OCR was enabled,
# is recorded as OCR-derived. This is an observable derivation, not a parser claim.
OCR_TEXT_LAYER_FLOOR = 8


def _docling_version() -> str:
    try:
        return metadata.version("docling")
    except metadata.PackageNotFoundError:  # pragma: no cover - packaging failure only
        return "unknown"


def _release_heap() -> None:
    """Hand a finished window's freed pages back to the operating system.

    Freeing the Python objects is not enough. glibc keeps the freed arenas for reuse, and with
    the many allocator threads a conversion runs the freed space fragments across them instead of
    being reused: measured across a 932-page textbook, worker RSS ratcheted ~6 MiB per page while
    the parse output actually retained grew ~0.2 MiB per page. `malloc_trim` is what returns
    those pages. It is a glibc facility, so this is a no-op anywhere it is not available — the
    parse is correct either way, only its memory profile differs.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):  # not glibc, or trimming unavailable
        return


def _source_text_chars(path: Path, window: tuple[int, int] | None = None) -> dict[int, int]:
    """Characters available from the PDF text layer alone, keyed by 1-based page number.

    `window` restricts extraction to a page range. Extracting a whole book at once is not
    bounded-memory: pypdf caches every page's decompressed content stream as it goes, and on a
    932-page textbook that transient cost measured ~1.3 GiB — paid before Docling had converted
    a single page, and never returned to the OS by the allocator. Reading only the pages of the
    window that needs them keeps this bounded by the same window size as conversion itself.
    """
    from pypdf import PdfReader

    counts: dict[int, int] = {}
    logging.disable(logging.ERROR)
    try:
        reader = PdfReader(str(path))
        first, last = window if window is not None else (1, len(reader.pages))
        for index in range(first, min(last, len(reader.pages)) + 1):
            try:
                counts[index] = len((reader.pages[index - 1].extract_text() or "").strip())
            except Exception:
                counts[index] = 0
    except Exception:
        return {}
    finally:
        logging.disable(logging.NOTSET)
    return counts


def _page_count(path: Path) -> int:
    from pypdf import PdfReader

    logging.disable(logging.ERROR)
    try:
        return len(PdfReader(str(path)).pages)
    except Exception as exc:
        raise ParserError(PARSER_SOURCE_CORRUPT, type(exc).__name__) from None
    finally:
        logging.disable(logging.NOTSET)


def _encode(image: Any, image_format: str) -> tuple[bytes, str, int, int] | None:
    if image is None:
        return None
    buffer = io.BytesIO()
    picture = image.convert("RGB") if image.mode in {"P", "RGBA", "LA"} else image
    picture.save(buffer, format=image_format.upper())
    return buffer.getvalue(), f"image/{image_format.lower()}", image.width, image.height


class DoclingDocumentParser:
    """`DocumentParser` implementation backed by Docling's standard PDF pipeline."""

    name = "docling"
    provider = "docling-project"

    def __init__(self) -> None:
        self.version = _docling_version()
        self._converters: dict[tuple[Any, ...], Any] = {}

    # ---------------------------------------------------------------- configuration

    def _pipeline_options(self, config: ParserConfig) -> tuple[Any, str | None]:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.pipeline_options import (
            OcrMode as DoclingOcrMode,
        )
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            RapidOcrOptions,
        )

        options = PdfPipelineOptions()
        # Pin the execution target. The layout model's output depends on reduction order, so an
        # unpinned device or thread count makes the same document parse differently per host.
        options.accelerator_options = AcceleratorOptions(
            num_threads=config.threads, device=AcceleratorDevice.CPU
        )
        options.document_timeout = float(config.timeout_seconds)
        options.do_table_structure = config.extract_tables
        options.do_formula_enrichment = config.extract_formulas
        options.generate_picture_images = config.extract_figures
        options.generate_page_images = config.generate_page_previews
        options.images_scale = config.preview_scale
        # No remote services and no external plugins: parsing must not call out of the worker.
        options.enable_remote_services = False
        options.allow_external_plugins = False
        options.do_picture_description = False
        options.do_picture_classification = False
        options.do_chart_extraction = False
        engine: str | None = None
        if config.ocr_mode == "OFF":
            options.do_ocr = False
        else:
            options.do_ocr = True
            options.ocr_options = RapidOcrOptions(
                mode=(
                    DoclingOcrMode.FULL_PAGE
                    if config.ocr_mode == "FORCE"
                    else DoclingOcrMode.DEFAULT
                )
            )
            engine = "rapidocr"
        return options, engine

    def _converter(self, config: ParserConfig) -> tuple[Any, str | None]:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options, engine = self._pipeline_options(config)
        key = (
            config.ocr_mode,
            config.extract_tables,
            config.extract_formulas,
            config.extract_figures,
            config.generate_page_previews,
            config.preview_scale,
            config.timeout_seconds,
            config.threads,
        )
        converter = self._converters.get(key)
        if converter is None:
            converter = DocumentConverter(
                allowed_formats=[InputFormat.PDF],
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
            )
            self._converters[key] = converter
        return converter, engine

    # ---------------------------------------------------------------- conversion

    def parse(
        self,
        source: ParseSource,
        config: ParserConfig,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ParsedDocument:
        """Convert a PDF.

        `on_progress(pages_done, pages_total)` is invoked between windows when windowing is
        active. It exists so the caller can renew its lease during a long conversion: without it
        the lease is only renewed at stage boundaries, so a worker killed mid-book leaves the job
        showing PARSING until the full lease elapses.
        """
        if not source.path.exists() or source.path.stat().st_size == 0:
            raise ParserError(PARSER_SOURCE_MISSING)
        try:
            import docling  # noqa: F401
        except ImportError:
            raise ParserError(PARSER_UNAVAILABLE) from None

        declared_pages = _page_count(source.path)
        if declared_pages > config.max_pages:
            raise ParserError(PARSER_PAGE_LIMIT_EXCEEDED, f"pages={declared_pages}")

        converter, engine = self._converter(config)
        windows = _windows(declared_pages, config)
        started = time.perf_counter()

        if windows is None:
            text_layer = _source_text_chars(source.path)
            document, warnings = self._convert(converter, source, config, None)
            pages = self._pages(document, config, text_layer)
            elements = self._elements(document, config)
            raw = self._raw_artifact(document, declared_pages, config)
        else:
            pages, elements, raw, warnings = self._convert_windowed(
                converter,
                source,
                config,
                windows,
                declared_pages,
                on_progress,
                started,
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        if duration_ms >= config.timeout_seconds * 1000 and not pages:
            raise ParserError(PARSER_TIMEOUT)

        ocr_pages = (
            frozenset(
                page.page_number
                for page in pages
                if page.source_text_chars < OCR_TEXT_LAYER_FLOOR
                and any(
                    element.page_number == page.page_number and (element.text or "").strip()
                    for element in elements
                )
            )
            if config.ocr_mode != "OFF"
            else frozenset()
        )
        return ParsedDocument(
            parser_name=self.name,
            parser_provider=self.provider,
            parser_version=self.version,
            pages=pages,
            elements=elements,
            raw_artifact=raw,
            source_page_count=declared_pages,
            ocr_engine=engine,
            ocr_pages=ocr_pages,
            warnings=tuple(warnings),
            duration_ms=duration_ms,
        )

    # ---------------------------------------------------------------- windowed conversion

    def _convert(
        self,
        converter: Any,
        source: ParseSource,
        config: ParserConfig,
        page_range: tuple[int, int] | None,
    ) -> tuple[Any, list[str]]:
        """One Docling conversion, with vendor exception types kept inside this adapter."""
        from docling.datamodel.base_models import ConversionStatus

        try:
            if page_range is None:
                result = converter.convert(str(source.path), raises_on_error=False)
            else:
                result = converter.convert(
                    str(source.path), raises_on_error=False, page_range=page_range
                )
        except MemoryError:
            raise ParserError(PARSER_OOM) from None
        except Exception as exc:  # Vendor exception types stay inside this adapter.
            raise ParserError(self._classify(exc), type(exc).__name__) from None

        warnings: list[str] = []
        if result.status == ConversionStatus.FAILURE:
            raise ParserError(PARSER_SOURCE_CORRUPT, "conversion_failure")
        if result.status == ConversionStatus.SKIPPED:
            raise ParserError(PARSER_UNSUPPORTED_PDF, "conversion_skipped")
        if result.status == ConversionStatus.PARTIAL_SUCCESS:
            warnings.append("PARSER_PARTIAL_SUCCESS")
        return result.document, warnings

    def _convert_windowed(
        self,
        converter: Any,
        source: ParseSource,
        config: ParserConfig,
        windows: tuple[tuple[int, int], ...],
        declared_pages: int,
        on_progress: Callable[[int, int], None] | None,
        started: float,
    ) -> tuple[tuple[ParsedPage, ...], tuple[ParsedElement, ...], bytes, list[str]]:
        """Convert page windows and join them into one document.

        Docling reports absolute page numbers under `page_range`, so page provenance needs no
        translation. What does need care is that every window restarts its own `#/texts/N`
        reference space and its own reading order: joined naively, the second window's
        `#/texts/0` would collide with the first window's and silently re-parent elements.
        References are therefore qualified by window and reading order continues across the
        join, so the result is addressed exactly as a single-pass parse would be."""
        pages: list[ParsedPage] = []
        elements: list[ParsedElement] = []
        artifacts: list[dict[str, Any]] = []
        warnings: list[str] = []
        order = 0
        seen: set[int] = set()

        budget = document_budget_seconds(
            declared_pages,
            config.timeout_seconds,
            config.document_seconds_per_page,
            config.max_document_timeout_seconds,
        )
        for index, (first, last) in enumerate(windows):
            if budget and time.perf_counter() - started >= budget:
                # Checked between windows rather than inside one: the conversion call is
                # opaque, so the whole-document budget can only be enforced at the seams.
                # Each window is separately bounded by Docling's own per-call timeout.
                raise ParserError(PARSER_TIMEOUT, f"pages={len(seen)}/{declared_pages}")
            document, window_warnings = self._convert(converter, source, config, (first, last))
            warnings.extend(w for w in window_warnings if w not in warnings)

            # Only this window's text layer, so the comparison against the parser's output
            # costs the same bounded amount of memory whatever the length of the book.
            text_layer = _source_text_chars(source.path, (first, last))
            for page in self._pages(document, config, text_layer):
                if page.page_number in seen:
                    # Docling honoured the range, so an overlap would mean the vendor returned
                    # pages outside it. Dropping the duplicate keeps page identity single-valued.
                    continue
                seen.add(page.page_number)
                pages.append(page)

            for element in self._elements(document, config):
                elements.append(_requalify(element, index, order))
                order += 1

            payload = json.loads(self._raw_artifact(document, declared_pages, config))
            artifacts.append({"window": [first, last], "document": payload.get("document")})
            # Release this window's Docling state before the next conversion allocates its own.
            del document
            del text_layer
            gc.collect()
            _release_heap()
            if on_progress is not None:
                on_progress(min(last, declared_pages), declared_pages)

        pages.sort(key=lambda page: page.page_number)
        if len(seen) < declared_pages:
            warnings.append("PARSER_WINDOW_PAGES_MISSING")
        raw = self._windowed_artifact(artifacts, declared_pages, config, windows)
        return tuple(pages), tuple(elements), raw, warnings

    def _windowed_artifact(
        self,
        artifacts: list[dict[str, Any]],
        source_pages: int,
        config: ParserConfig,
        windows: tuple[tuple[int, int], ...],
    ) -> bytes:
        """The structural record of a windowed parse.

        Each window's Docling export is kept whole and labelled with its page range rather than
        being fused into one synthetic document. A fabricated single export would claim a
        document Docling never produced, and the point of this artifact is to record exactly
        what the parser emitted."""
        envelope = {
            "artifact_schema": "medrag.parse.raw/1",
            "parser": {"name": self.name, "provider": self.provider, "version": self.version},
            "parser_config": {
                "ocr_mode": config.ocr_mode,
                "extract_tables": config.extract_tables,
                "extract_formulas": config.extract_formulas,
                "extract_figures": config.extract_figures,
                "generate_page_previews": config.generate_page_previews,
                "preview_scale": config.preview_scale,
            },
            "source_page_count": source_pages,
            "images_stored_separately": True,
            "windowed": {
                "page_window_size": config.page_window_size,
                "windows": [list(window) for window in windows],
            },
            "documents": artifacts,
        }
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()

    @staticmethod
    def _classify(exc: Exception) -> str:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if "timeout" in name or "timeout" in text:
            return PARSER_TIMEOUT
        if "memory" in name:
            return PARSER_OOM
        if "ocr" in text:
            return OCR_FAILED
        if "password" in text or "encrypt" in text:
            return PARSER_UNSUPPORTED_PDF
        return PARSER_INTERNAL_ERROR

    # ---------------------------------------------------------------- projection

    def _pages(
        self, document: Any, config: ParserConfig, text_layer: dict[int, int]
    ) -> tuple[ParsedPage, ...]:
        pages: list[ParsedPage] = []
        for page_no in sorted(document.pages):
            item = document.pages[page_no]
            size = item.size
            preview: bytes | None = None
            media_type: str | None = None
            if config.generate_page_previews and item.image is not None:
                try:
                    encoded = _encode(item.image.pil_image, config.preview_format)
                except Exception:
                    encoded = None  # A preview is optional; structure is not.
                if encoded:
                    preview, media_type = encoded[0], encoded[1]
            pages.append(
                ParsedPage(
                    page_number=int(page_no),
                    width=float(size.width),
                    height=float(size.height),
                    rotation=0,
                    source_text_chars=text_layer.get(int(page_no), 0),
                    preview_image=preview,
                    preview_media_type=media_type,
                )
            )
        return tuple(pages)

    def _elements(self, document: Any, config: ParserConfig) -> tuple[ParsedElement, ...]:
        from docling_core.types.doc.common.content_layer import ContentLayer

        # Re-exported by docling_core's public `document` module; the private per-item modules
        # are not a stable import path, so the vendor boundary accepts the untyped re-export.
        from docling_core.types.doc.document import (  # type: ignore[attr-defined]
            GroupItem,
            PictureItem,
            TableItem,
            TextItem,
        )

        elements: list[ParsedElement] = []
        siblings: dict[str, int] = {}
        order = 0
        layers = {ContentLayer.BODY, ContentLayer.FURNITURE}
        for item, depth in document.iterate_items(with_groups=True, included_content_layers=layers):
            reference = str(item.self_ref)
            if reference in {"#/body", "#/furniture"}:
                continue
            parent_reference = (
                str(item.parent.cref)
                if getattr(item, "parent", None) is not None
                and str(item.parent.cref) not in {"#/body", "#/furniture"}
                else None
            )
            label = str(getattr(item, "label", "") or "")
            if isinstance(item, GroupItem):
                element_type = GROUP_TYPES.get(label, ElementType.OTHER)
            else:
                element_type = LABEL_TYPES.get(label, ElementType.OTHER)
            page_number, bbox = self._provenance(item, document)
            ordinal = siblings.get(parent_reference or "", 0)
            siblings[parent_reference or ""] = ordinal + 1
            text = getattr(item, "text", None)
            caption_refs: tuple[str, ...] = ()
            caption_of: str | None = None
            table: ParsedTable | None = None
            figure: ParsedFigure | None = None
            formula: ParsedFormula | None = None
            metadata: dict[str, Any] = {}
            if isinstance(item, TableItem | PictureItem):
                caption_refs = tuple(str(ref.cref) for ref in item.captions)
            if isinstance(item, TableItem) and config.extract_tables:
                table = self._table(item, document)
            if isinstance(item, PictureItem) and config.extract_figures:
                figure = self._figure(item, document, config)
            if element_type is ElementType.FORMULA and isinstance(item, TextItem):
                formula = ParsedFormula(
                    source_expression=item.text or None,
                    notation="latex" if (item.text or "").strip() else None,
                )
            if isinstance(item, GroupItem):
                metadata["group"] = label
            elements.append(
                ParsedElement(
                    reference=reference,
                    element_type=element_type,
                    reading_order=order,
                    ordinal=ordinal,
                    depth=depth,
                    parent_reference=parent_reference,
                    page_number=page_number,
                    text=text,
                    bbox=bbox,
                    source_label=label or None,
                    content_layer=str(getattr(item, "content_layer", "") or "") or None,
                    caption_of=caption_of,
                    caption_references=caption_refs,
                    table=table,
                    figure=figure,
                    formula=formula,
                    metadata=metadata,
                )
            )
            order += 1
        return tuple(elements)

    def _provenance(self, item: Any, document: Any) -> tuple[int | None, BoundingBox | None]:
        provenance = getattr(item, "prov", None)
        if not provenance:
            return None, None
        first = provenance[0]
        page_no = int(first.page_no)
        page = document.pages.get(page_no)
        if page is None or page.size is None:
            return page_no, None
        box = first.bbox.to_top_left_origin(page_height=float(page.size.height))
        return page_no, BoundingBox(
            x1=float(box.l),
            y1=float(box.t),
            x2=float(box.r),
            y2=float(box.b),
            origin=CoordinateOrigin.TOPLEFT,
        )

    def _table(self, item: Any, document: Any) -> ParsedTable:
        data = item.data
        cells = tuple(
            ParsedTableCell(
                text=cell.text or "",
                row=int(cell.start_row_offset_idx),
                column=int(cell.start_col_offset_idx),
                row_span=max(1, int(cell.row_span or 1)),
                column_span=max(1, int(cell.col_span or 1)),
                is_column_header=bool(cell.column_header),
                is_row_header=bool(cell.row_header),
            )
            for cell in (data.table_cells or [])
        )
        header_rows = len({cell.row for cell in cells if cell.is_column_header})
        try:
            markdown = item.export_to_markdown(document)
        except Exception:
            markdown = None
        try:
            html = item.export_to_html(document)
        except Exception:
            html = None
        return ParsedTable(
            row_count=int(data.num_rows or 0),
            column_count=int(data.num_cols or 0),
            header_row_count=header_rows,
            cells=cells,
            markdown=markdown,
            html=html,
            metadata={"orientation": str(getattr(data, "orientation", "") or "")},
        )

    def _figure(self, item: Any, document: Any, config: ParserConfig) -> ParsedFigure:
        encoded: tuple[bytes, str, int, int] | None = None
        try:
            encoded = _encode(item.get_image(document), config.figure_format)
        except Exception:
            encoded = None
        return ParsedFigure(
            image=encoded[0] if encoded else None,
            media_type=encoded[1] if encoded else None,
            width=encoded[2] if encoded else None,
            height=encoded[3] if encoded else None,
            kind=str(item.label),
        )

    def _raw_artifact(self, document: Any, source_pages: int, config: ParserConfig) -> bytes:
        """Complete structured parser output.

        Binary page and picture rasters are removed here: they are persisted separately as
        immutable objects, so the JSON stays a faithful *structural* record instead of
        multiplying the artifact size with base64 copies of the same pixels.
        """
        payload = document.export_to_dict()
        for page in (payload.get("pages") or {}).values():
            if isinstance(page, dict):
                page.pop("image", None)
        for picture in payload.get("pictures") or []:
            if isinstance(picture, dict):
                picture.pop("image", None)
        for table in payload.get("tables") or []:
            if isinstance(table, dict):
                table.pop("image", None)
        envelope = {
            "artifact_schema": "medrag.parse.raw/1",
            "parser": {"name": self.name, "provider": self.provider, "version": self.version},
            "parser_config": {
                "ocr_mode": config.ocr_mode,
                "extract_tables": config.extract_tables,
                "extract_formulas": config.extract_formulas,
                "extract_figures": config.extract_figures,
                "generate_page_previews": config.generate_page_previews,
                "preview_scale": config.preview_scale,
            },
            "source_page_count": source_pages,
            "images_stored_separately": True,
            "document": payload,
        }
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()


def _windows(declared_pages: int, config: ParserConfig) -> tuple[tuple[int, int], ...] | None:
    """Page ranges to convert, or None to convert the whole document in one call.

    Short documents keep the single-call path, so their output — and the determinism the M2
    evaluation asserts over the fixture corpus — is unchanged. Windowing takes over only where
    the single-call path is the thing that fails."""
    size = config.page_window_size
    if size <= 0 or declared_pages <= max(config.page_window_threshold, size):
        return None
    return tuple(
        (start, min(start + size - 1, declared_pages))
        for start in range(1, declared_pages + 1, size)
    )


def _requalify(element: ParsedElement, window: int, order: int) -> ParsedElement:
    """Namespace one window's references and place it in the joined reading order.

    Every window restarts Docling's `#/texts/N` numbering, so references are prefixed with the
    window index. Parent and caption references take the same prefix, which keeps every
    relationship inside the window where it was actually established while making the joined
    document's reference space single-valued."""
    tag = f"w{window}"

    def qualify(reference: str | None) -> str | None:
        return None if reference is None else f"{tag}{reference}"

    return replace(
        element,
        reference=f"{tag}{element.reference}",
        parent_reference=qualify(element.parent_reference),
        caption_of=qualify(element.caption_of),
        caption_references=tuple(f"{tag}{ref}" for ref in element.caption_references),
        reading_order=order,
    )
