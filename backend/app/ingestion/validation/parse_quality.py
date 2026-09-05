"""Deterministic parse-quality validation.

A successful parser run is not a valid parse. These rules measure observable properties of the
normalized output against typed thresholds and produce findings plus one structured result. No
rule estimates an "accuracy percentage" and no rule asks a model whether the text looks right.
"""

from dataclasses import dataclass, field
from typing import Any

from app.core.parsing_config import ParseThresholds
from app.models.enums import ElementType, ParseResult, Severity

# --- Finding codes -----------------------------------------------------------------------
PAGE_COUNT_MISMATCH = "PAGE_COUNT_MISMATCH"
NO_PAGES_PARSED = "NO_PAGES_PARSED"
NO_ELEMENTS_PARSED = "NO_ELEMENTS_PARSED"
LOW_NON_EMPTY_PAGE_RATIO = "LOW_NON_EMPTY_PAGE_RATIO"
LOW_ELEMENT_DENSITY = "LOW_ELEMENT_DENSITY"
HIGH_INVALID_BBOX_RATIO = "HIGH_INVALID_BBOX_RATIO"
HIGH_UNLOCATED_ELEMENT_RATIO = "HIGH_UNLOCATED_ELEMENT_RATIO"
READING_ORDER_NOT_CONTIGUOUS = "READING_ORDER_NOT_CONTIGUOUS"
READING_ORDER_PAGE_REGRESSION = "READING_ORDER_PAGE_REGRESSION"
HIGH_MALFORMED_TABLE_RATIO = "HIGH_MALFORMED_TABLE_RATIO"
HIGH_SUSPICIOUS_OCR_PAGE_RATIO = "HIGH_SUSPICIOUS_OCR_PAGE_RATIO"
HIGH_EMPTY_FORMULA_RATIO = "HIGH_EMPTY_FORMULA_RATIO"
PARSER_PARTIAL_SUCCESS = "PARSER_PARTIAL_SUCCESS"

PAGE_EMPTY = "PAGE_EMPTY"
PAGE_CONTENT_LOST = "PAGE_CONTENT_LOST"
PAGE_INVALID_DIMENSIONS = "PAGE_INVALID_DIMENSIONS"
PAGE_PREVIEW_MISSING = "PAGE_PREVIEW_MISSING"
PAGE_OCR_SUSPICIOUS = "PAGE_OCR_SUSPICIOUS"

ELEMENT_BBOX_MALFORMED = "ELEMENT_BBOX_MALFORMED"
ELEMENT_BBOX_OUT_OF_PAGE = "ELEMENT_BBOX_OUT_OF_PAGE"
ELEMENT_MISSING_PAGE = "ELEMENT_MISSING_PAGE"
TABLE_EMPTY = "TABLE_EMPTY"
TABLE_STRUCTURE_MALFORMED = "TABLE_STRUCTURE_MALFORMED"
TABLE_CAPTION_UNRESOLVED = "TABLE_CAPTION_UNRESOLVED"
FIGURE_ARTIFACT_MISSING = "FIGURE_ARTIFACT_MISSING"
FIGURE_CAPTION_UNRESOLVED = "FIGURE_CAPTION_UNRESOLVED"
FORMULA_EMPTY = "FORMULA_EMPTY"

MESSAGES: dict[str, str] = {
    PAGE_COUNT_MISMATCH: "Parsed page count does not match the source page count.",
    NO_PAGES_PARSED: "The parser produced no pages for this document.",
    NO_ELEMENTS_PARSED: "The parser produced no structural elements for this document.",
    LOW_NON_EMPTY_PAGE_RATIO: "Too many pages contain almost no extracted text.",
    LOW_ELEMENT_DENSITY: "The document yielded very few elements per page.",
    HIGH_INVALID_BBOX_RATIO: "Too many elements have unusable page coordinates.",
    HIGH_UNLOCATED_ELEMENT_RATIO: "Too many elements could not be associated with a page.",
    READING_ORDER_NOT_CONTIGUOUS: "Reading order is not a contiguous sequence.",
    READING_ORDER_PAGE_REGRESSION: (
        "Reading order revisits earlier pages an unusual number of times."
    ),
    HIGH_MALFORMED_TABLE_RATIO: "Too many tables have unusable structure.",
    HIGH_SUSPICIOUS_OCR_PAGE_RATIO: "Too many pages depend on low-confidence OCR output.",
    HIGH_EMPTY_FORMULA_RATIO: "Too many detected formulas have no recoverable expression.",
    PARSER_PARTIAL_SUCCESS: "The parser reported partial success for this document.",
    PAGE_EMPTY: "This page yielded almost no extracted text.",
    PAGE_CONTENT_LOST: "This page has a source text layer but produced no parsed content.",
    PAGE_INVALID_DIMENSIONS: "This page reports invalid dimensions.",
    PAGE_PREVIEW_MISSING: "No preview image was generated for this page.",
    PAGE_OCR_SUSPICIOUS: "This page has no text layer and produced very little OCR text.",
    ELEMENT_BBOX_MALFORMED: "This element has a malformed bounding box.",
    ELEMENT_BBOX_OUT_OF_PAGE: "This element bounding box falls outside the page.",
    ELEMENT_MISSING_PAGE: "This element has no page association.",
    TABLE_EMPTY: "This table has no rows or no columns.",
    TABLE_STRUCTURE_MALFORMED: "This table's cells do not fit its declared grid.",
    TABLE_CAPTION_UNRESOLVED: "This table references a caption that was not found.",
    FIGURE_ARTIFACT_MISSING: "No image artifact was stored for this figure.",
    FIGURE_CAPTION_UNRESOLVED: "This figure references a caption that was not found.",
    FORMULA_EMPTY: "This formula has no recoverable expression.",
}


@dataclass(frozen=True)
class Finding:
    scope: str  # "document" | "page" | "element"
    severity: Severity
    code: str
    page_number: int | None = None
    element_reference: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def message(self) -> str:
        return MESSAGES.get(self.code, "Parse quality finding.")


@dataclass(frozen=True)
class PageFacts:
    page_number: int
    width: float
    height: float
    text_chars: int
    element_count: int
    source_text_chars: int
    ocr_used: bool
    has_preview: bool


@dataclass(frozen=True)
class ElementFacts:
    reference: str
    element_type: ElementType
    reading_order: int
    page_number: int | None
    has_bbox: bool
    bbox_valid: bool
    bbox_in_page: bool
    # A container groups other elements and carries no page or geometry of its own.
    is_container: bool = False


@dataclass(frozen=True)
class TableFacts:
    reference: str
    page_number: int | None
    row_count: int
    column_count: int
    cell_count: int
    max_row_index: int
    max_column_index: int
    caption_resolved: bool
    caption_referenced: bool


@dataclass(frozen=True)
class FigureFacts:
    reference: str
    page_number: int | None
    has_image: bool
    caption_resolved: bool
    caption_referenced: bool


@dataclass(frozen=True)
class FormulaFacts:
    reference: str
    page_number: int | None
    has_expression: bool


@dataclass(frozen=True)
class ParseFacts:
    source_page_count: int | None
    previews_requested: bool
    figures_requested: bool
    parser_warnings: tuple[str, ...]
    pages: tuple[PageFacts, ...]
    elements: tuple[ElementFacts, ...]
    tables: tuple[TableFacts, ...]
    figures: tuple[FigureFacts, ...]
    formulas: tuple[FormulaFacts, ...]


@dataclass(frozen=True)
class ValidationOutcome:
    result: ParseResult
    findings: tuple[Finding, ...]

    @property
    def blocking(self) -> bool:
        return self.result in {ParseResult.FAIL, ParseResult.NEEDS_REVIEW}


def _ratio(count: int, total: int) -> float:
    return (count / total) if total else 0.0


def validate(facts: ParseFacts, thresholds: ParseThresholds) -> ValidationOutcome:
    findings: list[Finding] = []
    pages, elements = facts.pages, facts.elements

    for warning in facts.parser_warnings:
        if warning == PARSER_PARTIAL_SUCCESS:
            findings.append(Finding("document", Severity.WARNING, PARSER_PARTIAL_SUCCESS))

    if not pages:
        findings.append(Finding("document", Severity.CRITICAL, NO_PAGES_PARSED))
        return ValidationOutcome(ParseResult.FAIL, tuple(findings))
    if not elements:
        findings.append(Finding("document", Severity.CRITICAL, NO_ELEMENTS_PARSED))

    # --- page-level ------------------------------------------------------------------
    for page in pages:
        if page.width <= 0 or page.height <= 0:
            findings.append(
                Finding("page", Severity.ERROR, PAGE_INVALID_DIMENSIONS, page.page_number)
            )
        if page.text_chars < thresholds.min_chars_per_page:
            severity = (
                Severity.ERROR
                if page.source_text_chars >= thresholds.min_chars_per_page
                else Severity.WARNING
            )
            code = PAGE_CONTENT_LOST if severity is Severity.ERROR else PAGE_EMPTY
            findings.append(
                Finding(
                    "page",
                    severity,
                    code,
                    page.page_number,
                    details={
                        "parsed_chars": page.text_chars,
                        "source_text_chars": page.source_text_chars,
                    },
                )
            )
        if page.ocr_used and page.text_chars < thresholds.min_chars_per_page:
            findings.append(
                Finding("page", Severity.WARNING, PAGE_OCR_SUSPICIOUS, page.page_number)
            )
        if facts.previews_requested and not page.has_preview:
            findings.append(Finding("page", Severity.INFO, PAGE_PREVIEW_MISSING, page.page_number))

    # --- element-level ---------------------------------------------------------------
    invalid_bbox = 0
    unlocated = 0
    locatable = [element for element in elements if not element.is_container]
    for element in elements:
        if element.page_number is None:
            if element.is_container:
                continue
            unlocated += 1
            findings.append(
                Finding("element", Severity.INFO, ELEMENT_MISSING_PAGE, None, element.reference)
            )
            continue
        if element.has_bbox and not element.bbox_valid:
            invalid_bbox += 1
            findings.append(
                Finding(
                    "element",
                    Severity.WARNING,
                    ELEMENT_BBOX_MALFORMED,
                    element.page_number,
                    element.reference,
                )
            )
        elif element.has_bbox and not element.bbox_in_page:
            invalid_bbox += 1
            findings.append(
                Finding(
                    "element",
                    Severity.WARNING,
                    ELEMENT_BBOX_OUT_OF_PAGE,
                    element.page_number,
                    element.reference,
                )
            )

    # --- artifact-level --------------------------------------------------------------
    malformed_tables = 0
    for table in facts.tables:
        if table.row_count == 0 or table.column_count == 0:
            malformed_tables += 1
            findings.append(
                Finding(
                    "element", Severity.WARNING, TABLE_EMPTY, table.page_number, table.reference
                )
            )
        elif (
            table.cell_count == 0
            or table.max_row_index >= table.row_count
            or table.max_column_index >= table.column_count
        ):
            malformed_tables += 1
            findings.append(
                Finding(
                    "element",
                    Severity.WARNING,
                    TABLE_STRUCTURE_MALFORMED,
                    table.page_number,
                    table.reference,
                    {
                        "declared_rows": table.row_count,
                        "declared_columns": table.column_count,
                        "cells": table.cell_count,
                    },
                )
            )
        if table.caption_referenced and not table.caption_resolved:
            findings.append(
                Finding(
                    "element",
                    Severity.INFO,
                    TABLE_CAPTION_UNRESOLVED,
                    table.page_number,
                    table.reference,
                )
            )

    for figure in facts.figures:
        if facts.figures_requested and not figure.has_image:
            findings.append(
                Finding(
                    "element",
                    Severity.WARNING,
                    FIGURE_ARTIFACT_MISSING,
                    figure.page_number,
                    figure.reference,
                )
            )
        if figure.caption_referenced and not figure.caption_resolved:
            findings.append(
                Finding(
                    "element",
                    Severity.INFO,
                    FIGURE_CAPTION_UNRESOLVED,
                    figure.page_number,
                    figure.reference,
                )
            )

    empty_formulas = 0
    for formula in facts.formulas:
        if not formula.has_expression:
            empty_formulas += 1
            findings.append(
                Finding(
                    "element",
                    Severity.WARNING,
                    FORMULA_EMPTY,
                    formula.page_number,
                    formula.reference,
                )
            )

    # --- document-level aggregates ---------------------------------------------------
    page_total = len(pages)
    non_empty = sum(1 for page in pages if page.text_chars >= thresholds.min_chars_per_page)
    non_empty_ratio = _ratio(non_empty, page_total)
    if non_empty_ratio < thresholds.min_non_empty_page_ratio:
        findings.append(
            Finding(
                "document",
                Severity.ERROR,
                LOW_NON_EMPTY_PAGE_RATIO,
                details={
                    "non_empty_pages": non_empty,
                    "pages": page_total,
                    "threshold": thresholds.min_non_empty_page_ratio,
                },
            )
        )
    if _ratio(len(elements), page_total) < thresholds.min_elements_per_page:
        findings.append(
            Finding(
                "document",
                Severity.WARNING,
                LOW_ELEMENT_DENSITY,
                details={"elements": len(elements), "pages": page_total},
            )
        )
    if _ratio(invalid_bbox, len(elements)) > thresholds.max_invalid_bbox_ratio:
        findings.append(
            Finding(
                "document",
                Severity.ERROR,
                HIGH_INVALID_BBOX_RATIO,
                details={"invalid": invalid_bbox, "elements": len(elements)},
            )
        )
    if _ratio(unlocated, len(locatable)) > thresholds.max_unlocated_element_ratio:
        findings.append(
            Finding(
                "document",
                Severity.WARNING,
                HIGH_UNLOCATED_ELEMENT_RATIO,
                details={"unlocated": unlocated, "locatable_elements": len(locatable)},
            )
        )
    if _ratio(malformed_tables, len(facts.tables)) > thresholds.max_malformed_table_ratio:
        findings.append(
            Finding(
                "document",
                Severity.ERROR,
                HIGH_MALFORMED_TABLE_RATIO,
                details={"malformed": malformed_tables, "tables": len(facts.tables)},
            )
        )
    suspicious_ocr = sum(
        1 for page in pages if page.ocr_used and page.text_chars < thresholds.min_chars_per_page
    )
    if _ratio(suspicious_ocr, page_total) > thresholds.max_suspicious_ocr_page_ratio:
        findings.append(
            Finding(
                "document",
                Severity.ERROR,
                HIGH_SUSPICIOUS_OCR_PAGE_RATIO,
                details={"pages": page_total, "suspicious": suspicious_ocr},
            )
        )
    if _ratio(empty_formulas, len(facts.formulas)) > thresholds.max_empty_formula_ratio:
        findings.append(
            Finding(
                "document",
                Severity.WARNING,
                HIGH_EMPTY_FORMULA_RATIO,
                details={"empty": empty_formulas, "formulas": len(facts.formulas)},
            )
        )

    orders = sorted(element.reading_order for element in elements)
    if orders and orders != list(range(len(orders))):
        findings.append(Finding("document", Severity.CRITICAL, READING_ORDER_NOT_CONTIGUOUS))
    regressions = _page_regressions(elements)
    if regressions > page_total:
        findings.append(
            Finding(
                "document",
                Severity.WARNING,
                READING_ORDER_PAGE_REGRESSION,
                details={"regressions": regressions, "pages": page_total},
            )
        )

    if facts.source_page_count is not None and facts.source_page_count != page_total:
        findings.append(
            Finding(
                "document",
                Severity.CRITICAL if not page_total else Severity.ERROR,
                PAGE_COUNT_MISMATCH,
                details={"source": facts.source_page_count, "parsed": page_total},
            )
        )

    return ValidationOutcome(_result(findings, thresholds), tuple(findings))


def _page_regressions(elements: tuple[ElementFacts, ...]) -> int:
    """Count reading-order steps that move backwards to an earlier page.

    Multi-column and floating content legitimately produce a few; a count exceeding the page
    count indicates the traversal is not a plausible reading order.
    """
    regressions = 0
    previous: int | None = None
    for element in sorted(elements, key=lambda item: item.reading_order):
        if element.page_number is None:
            continue
        if previous is not None and element.page_number < previous:
            regressions += 1
        previous = element.page_number
    return regressions


def _result(findings: list[Finding], thresholds: ParseThresholds) -> ParseResult:
    codes = {finding.code for finding in findings}
    severities = {finding.severity for finding in findings}
    if Severity.CRITICAL in severities:
        return ParseResult.FAIL
    if PAGE_COUNT_MISMATCH in codes and thresholds.review_on_page_count_mismatch:
        return ParseResult.NEEDS_REVIEW
    if Severity.ERROR in severities:
        # An ERROR means measurable content was lost or misplaced, at any scope. Such a parse
        # never proceeds unreviewed; only INFO/WARNING anomalies pass with warnings.
        return ParseResult.NEEDS_REVIEW
    # PASS means a parse with nothing to report at all. An INFO finding (an absent optional
    # preview, an unlocated element) still means an operator has something to look at.
    return ParseResult.PASS_WITH_WARNINGS if findings else ParseResult.PASS
