"""M2 unit tests.

These exercise the parser-independent layer only: normalization, transitions, coordinate
conversion, validation rules, error classification and configuration identity. Nothing here
imports Docling or touches PostgreSQL, MinIO or Redis.
"""

import json
from pathlib import Path

import pytest
from app.core.errors import DomainError
from app.core.parsing_config import ParseThresholds, ParsingConfig
from app.ingestion.normalizer.text import (
    changed,
    normalize_expression,
    normalize_text,
    page_text,
)
from app.ingestion.parser import errors as parse_errors
from app.ingestion.parser.model import (
    BoundingBox,
    ParsedDocument,
    ParsedElement,
    ParsedPage,
    page_index,
)
from app.ingestion.state import TRANSITIONS, require_transition
from app.ingestion.validation import parse_quality as quality
from app.models.enums import (
    CoordinateOrigin,
    ElementType,
    OcrMode,
    ParseResult,
    Severity,
    Status,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).parent / "fixtures/parsing"

# --------------------------------------------------------------------------- state machine


@pytest.mark.parametrize(
    "before,after",
    [
        ("QUEUED", "PARSING"),
        ("PARSING", "NORMALIZING"),
        ("NORMALIZING", "ENRICHING"),
        ("ENRICHING", "READY_FOR_CHUNKING"),
        ("PARSING", "FAILED"),
        ("NORMALIZING", "CANCELLED"),
        ("ENRICHING", "NEEDS_REVIEW"),
        ("READY_FOR_CHUNKING", "CANCELLED"),
    ],
)
def test_parse_transitions_allowed(before, after):
    require_transition(Status(before), Status(after))


@pytest.mark.parametrize(
    "before,after",
    [
        ("QUEUED", "NORMALIZING"),
        ("PARSING", "ENRICHING"),
        ("PARSING", "READY_FOR_CHUNKING"),
        ("ENRICHING", "CHUNKING"),
        ("READY_FOR_CHUNKING", "READY"),
        ("READY_FOR_CHUNKING", "READY_FOR_EMBEDDING"),
        ("READY_FOR_EMBEDDING", "INDEXING"),
        ("VERIFYING_INDEX", "READY"),
        ("READY_FOR_RETRIEVAL", "READY"),
        ("READY_FOR_CHUNKING", "PARSING"),
        ("QUEUED", "NEEDS_REVIEW"),
    ],
)
def test_parse_transitions_rejected(before, after):
    with pytest.raises(DomainError, match="INGESTION_INVALID_TRANSITION"):
        require_transition(Status(before), Status(after))


def test_m5_states_are_not_executable():
    """The pipeline now stops at READY_FOR_RETRIEVAL, and READY remains unreachable.

    The boundary has moved once per milestone, deliberately and with a reviewed migration each
    time: M2 stopped at READY_FOR_CHUNKING, M3 at READY_FOR_EMBEDDING, M4 at READY_FOR_RETRIEVAL.
    READY is reserved for a version that is genuinely answerable, which needs query retrieval,
    grounding and verification. None of those exist, so nothing may enter it.
    """
    reachable = {target for targets in TRANSITIONS.values() for target in targets}
    assert Status.READY not in reachable
    assert Status.READY not in TRANSITIONS
    # READY_FOR_RETRIEVAL means the index verified, not that the document can be answered from.
    assert TRANSITIONS[Status.READY_FOR_RETRIEVAL] == frozenset({Status.CANCELLED})


# --------------------------------------------------------------------------- normalization


def test_normalization_repairs_extraction_artefacts():
    raw = "Hyper­tension is de-\nfined as ﬁbrosis​ with spacing."
    assert normalize_text(raw) == "Hypertension is defined as fibrosis with spacing."


def test_normalization_preserves_paragraph_breaks_and_trims():
    assert normalize_text("Line one\nline two\n\n\n\nNext") == "Line one line two\n\nNext"
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


@pytest.mark.parametrize(
    "value",
    [
        "Give 12.5 mg/kg every 8 hours",
        "0.9% NaCl 500 mL",
        "HbA1c 6.5%",
        "1,000,000 units",
        "pO2 / FiO2 ratio 300",
    ],
)
def test_normalization_never_rewrites_clinical_looking_values(value):
    """Numbers, units and terminology survive untouched; plausibility is never enforced."""
    assert normalize_text(value) == value


def test_formula_expression_is_only_whitespace_normalized():
    assert normalize_expression("  C = \\frac { A \\cdot B } { D }  ") == (
        "C = \\frac { A \\cdot B } { D }"
    )
    assert normalize_expression(None) is None
    assert normalize_expression("   ") is None


def test_changed_flag_and_page_rollup():
    assert changed("a­a", normalize_text("a­a"))
    assert not changed("plain", normalize_text("plain"))
    assert page_text(["first", "", "second"]) == "first\n\nsecond"


# --------------------------------------------------------------------------- geometry


def test_bounding_box_validity_and_containment():
    box = BoundingBox(x1=10, y1=20, x2=110, y2=60, origin=CoordinateOrigin.TOPLEFT)
    assert box.valid and box.within(612, 792)
    assert not BoundingBox(x1=10, y1=20, x2=5, y2=60).valid
    assert not BoundingBox(x1=-1, y1=20, x2=100, y2=60).valid
    assert not box.within(50, 792)


def test_page_numbering_is_one_based_and_indexed_by_printed_number():
    pages = (ParsedPage(page_number=1, width=612, height=792),)
    assert page_index(pages)[1].width == 612
    assert 0 not in page_index(pages)


# --------------------------------------------------------------------------- error model


def test_deterministic_input_failures_are_not_retried():
    for code in (
        parse_errors.PARSER_SOURCE_CORRUPT,
        parse_errors.PARSER_UNSUPPORTED_PDF,
        parse_errors.PARSER_PAGE_LIMIT_EXCEEDED,
        parse_errors.NORMALIZATION_INVALID_STRUCTURE,
        parse_errors.PARSE_VALIDATION_FAILED,
    ):
        assert not parse_errors.is_retryable(code)


def test_environmental_failures_are_retryable():
    for code in (
        parse_errors.PARSER_TIMEOUT,
        parse_errors.PARSER_OOM,
        parse_errors.RAW_ARTIFACT_STORAGE_FAILED,
        parse_errors.PARSER_LEASE_EXPIRED,
    ):
        assert parse_errors.is_retryable(code)


def test_unknown_error_code_stays_visible_and_message_is_safe():
    error = parse_errors.ParserError("SOMETHING_NEW", detail="internal detail")
    assert not error.retryable
    assert error.message == "Parsing failed."
    assert "internal detail" not in error.message


def test_every_declared_code_has_a_safe_message():
    for code in parse_errors.RETRYABLE | parse_errors.TERMINAL:
        assert parse_errors.safe_message(code) != "Parsing failed."


# --------------------------------------------------------------------------- configuration


def test_config_fingerprint_changes_with_any_policy_field():
    base = ParsingConfig()
    assert base.fingerprint == ParsingConfig().fingerprint
    assert base.fingerprint != ParsingConfig(ocr_mode=OcrMode.FORCE).fingerprint
    # A silently edited threshold must not reuse a parse produced under the old rule.
    tightened = ParsingConfig(thresholds=ParseThresholds(min_chars_per_page=200))
    assert base.fingerprint != tightened.fingerprint
    assert base.version == tightened.version


def test_config_is_frozen_and_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ParsingConfig(unknown_field=1)
    with pytest.raises(ValidationError):
        ParsingConfig().ocr_mode = OcrMode.OFF


# --------------------------------------------------------------------------- validation rules


def _page(number=1, chars=500, source=500, ocr=False, preview=True, width=612.0, height=792.0):
    return quality.PageFacts(
        page_number=number,
        width=width,
        height=height,
        text_chars=chars,
        element_count=5,
        source_text_chars=source,
        ocr_used=ocr,
        has_preview=preview,
    )


def _element(order=0, page=1, valid=True, in_page=True, container=False):
    return quality.ElementFacts(
        reference=f"#/texts/{order}",
        element_type=ElementType.PARAGRAPH,
        reading_order=order,
        page_number=page,
        has_bbox=not container,
        bbox_valid=valid,
        bbox_in_page=in_page,
        is_container=container,
    )


def _facts(**overrides):
    base = dict(
        source_page_count=1,
        previews_requested=True,
        figures_requested=True,
        parser_warnings=(),
        pages=(_page(),),
        elements=(_element(0), _element(1)),
        tables=(),
        figures=(),
        formulas=(),
    )
    base.update(overrides)
    return quality.ParseFacts(**base)


THRESHOLDS = ParseThresholds()


def test_clean_parse_passes():
    outcome = quality.validate(_facts(), THRESHOLDS)
    assert outcome.result is ParseResult.PASS
    assert outcome.findings == ()
    assert not outcome.blocking


def test_no_pages_fails_closed():
    outcome = quality.validate(_facts(pages=(), elements=()), THRESHOLDS)
    assert outcome.result is ParseResult.FAIL
    assert outcome.findings[0].code == quality.NO_PAGES_PARSED


def test_page_count_mismatch_requires_review():
    outcome = quality.validate(_facts(source_page_count=4), THRESHOLDS)
    assert outcome.result is ParseResult.NEEDS_REVIEW
    assert any(f.code == quality.PAGE_COUNT_MISMATCH for f in outcome.findings)


def test_page_with_text_layer_but_no_parsed_content_is_an_error():
    outcome = quality.validate(_facts(pages=(_page(chars=0, source=900),)), THRESHOLDS)
    codes = {f.code for f in outcome.findings}
    assert quality.PAGE_CONTENT_LOST in codes
    assert outcome.result is ParseResult.NEEDS_REVIEW


def test_genuinely_blank_page_is_only_a_warning():
    outcome = quality.validate(
        _facts(pages=(_page(chars=0, source=0), _page(number=2)), source_page_count=2), THRESHOLDS
    )
    codes = {f.code for f in outcome.findings}
    assert quality.PAGE_EMPTY in codes
    assert quality.PAGE_CONTENT_LOST not in codes


def test_widespread_ocr_failure_requires_review():
    pages = tuple(_page(number=n, chars=0, source=0, ocr=True) for n in range(1, 5))
    outcome = quality.validate(_facts(pages=pages, source_page_count=4), THRESHOLDS)
    codes = {f.code for f in outcome.findings}
    assert quality.HIGH_SUSPICIOUS_OCR_PAGE_RATIO in codes
    assert outcome.result is ParseResult.NEEDS_REVIEW


def test_malformed_bounding_boxes_are_reported():
    elements = tuple(_element(order=n, valid=False) for n in range(4))
    outcome = quality.validate(_facts(elements=elements), THRESHOLDS)
    codes = {f.code for f in outcome.findings}
    assert quality.ELEMENT_BBOX_MALFORMED in codes
    assert quality.HIGH_INVALID_BBOX_RATIO in codes


def test_bounding_box_outside_the_page_is_reported():
    outcome = quality.validate(_facts(elements=(_element(0, in_page=False),)), THRESHOLDS)
    assert any(f.code == quality.ELEMENT_BBOX_OUT_OF_PAGE for f in outcome.findings)


def test_structural_containers_do_not_count_as_unlocated():
    elements = tuple(
        [_element(order=n) for n in range(8)] + [_element(order=8, page=None, container=True)]
    )
    outcome = quality.validate(_facts(elements=elements), THRESHOLDS)
    codes = {f.code for f in outcome.findings}
    assert quality.ELEMENT_MISSING_PAGE not in codes
    assert quality.HIGH_UNLOCATED_ELEMENT_RATIO not in codes


def test_non_contiguous_reading_order_fails_closed():
    elements = (_element(0), _element(5))
    outcome = quality.validate(_facts(elements=elements), THRESHOLDS)
    assert any(f.code == quality.READING_ORDER_NOT_CONTIGUOUS for f in outcome.findings)
    assert outcome.result is ParseResult.FAIL


def test_empty_table_is_flagged_without_discarding_it():
    table = quality.TableFacts(
        reference="#/tables/0",
        page_number=1,
        row_count=0,
        column_count=0,
        cell_count=0,
        max_row_index=-1,
        max_column_index=-1,
        caption_resolved=False,
        caption_referenced=False,
    )
    outcome = quality.validate(_facts(tables=(table,)), THRESHOLDS)
    codes = {f.code for f in outcome.findings}
    assert quality.TABLE_EMPTY in codes
    assert quality.HIGH_MALFORMED_TABLE_RATIO in codes


def test_table_cells_outside_the_declared_grid_are_malformed():
    table = quality.TableFacts(
        reference="#/tables/0",
        page_number=1,
        row_count=2,
        column_count=2,
        cell_count=4,
        max_row_index=7,
        max_column_index=1,
        caption_resolved=True,
        caption_referenced=True,
    )
    outcome = quality.validate(_facts(tables=(table,)), THRESHOLDS)
    assert any(f.code == quality.TABLE_STRUCTURE_MALFORMED for f in outcome.findings)


def test_missing_figure_artifact_and_unresolved_caption_are_reported():
    figure = quality.FigureFacts(
        reference="#/pictures/0",
        page_number=1,
        has_image=False,
        caption_resolved=False,
        caption_referenced=True,
    )
    outcome = quality.validate(_facts(figures=(figure,)), THRESHOLDS)
    codes = {f.code for f in outcome.findings}
    assert quality.FIGURE_ARTIFACT_MISSING in codes
    assert quality.FIGURE_CAPTION_UNRESOLVED in codes


def test_empty_formula_is_reported_but_never_reconstructed():
    formula = quality.FormulaFacts(reference="#/texts/3", page_number=1, has_expression=False)
    outcome = quality.validate(_facts(formulas=(formula,)), THRESHOLDS)
    codes = {f.code for f in outcome.findings}
    assert quality.FORMULA_EMPTY in codes
    assert quality.HIGH_EMPTY_FORMULA_RATIO in codes


def test_optional_preview_failure_is_informational_only():
    outcome = quality.validate(_facts(pages=(_page(preview=False),)), THRESHOLDS)
    assert any(f.code == quality.PAGE_PREVIEW_MISSING for f in outcome.findings)
    assert outcome.result is ParseResult.PASS_WITH_WARNINGS


def test_every_finding_message_is_operator_safe():
    """Findings must never contain document text; they carry codes, counts and page numbers."""
    for message in quality.MESSAGES.values():
        assert message and message[0].isupper()
        assert "%" not in message  # no fabricated accuracy figures
    assert quality.Finding("document", Severity.INFO, "UNKNOWN_CODE").message == (
        "Parse quality finding."
    )


def test_element_type_vocabulary_keeps_structure_distinct():
    """Structure is never collapsed into undifferentiated text."""
    for required in (
        "TITLE",
        "HEADING",
        "PARAGRAPH",
        "LIST",
        "LIST_ITEM",
        "TABLE",
        "FORMULA",
        "FIGURE",
        "CAPTION",
        "FOOTNOTE",
        "PAGE_HEADER",
        "PAGE_FOOTER",
        "OTHER",
    ):
        assert ElementType(required)


def test_parsed_element_defaults_leave_unknown_structure_unknown():
    element = ParsedElement(
        reference="#/texts/0",
        element_type=ElementType.PARAGRAPH,
        reading_order=0,
        ordinal=0,
        depth=1,
    )
    assert element.parent_reference is None
    assert element.page_number is None
    assert element.bbox is None
    assert element.caption_references == ()


# --------------------------------------------------------------------------- parsing evaluation


def test_gold_dataset_is_loadable_and_covers_the_structural_cases():
    from app.evaluation.parsing import load_gold

    cases = load_gold(Path("docs/evals/parsing-gold.json"))
    names = {case.name for case in cases}
    for required in (
        "basic-text",
        "multi-page",
        "table",
        "continued-table",
        "figure",
        "formula",
        "two-column",
        "question-bank",
        "scanned-like",
    ):
        assert required in names
    for case in cases:
        assert (FIXTURES / case.document).exists(), case.document
        assert case.pages >= 1 and case.description


def test_gold_schema_version_is_enforced(tmp_path):
    from app.evaluation.parsing import load_gold

    path = tmp_path / "gold.json"
    path.write_text(json.dumps({"schema": "other/9", "cases": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported gold dataset schema"):
        load_gold(path)


def test_evaluator_detects_lost_structure():
    """The evaluation must fail when extraction degrades, not average the loss away."""
    from app.evaluation.parsing import GoldCase, evaluate_case

    case = GoldCase(
        name="synthetic",
        document="none.pdf",
        description="one page, one table",
        pages=1,
        tables=1,
        table_grid=[[2, 2, 4]],
        required_text=["expected fragment"],
    )
    document = ParsedDocument(
        parser_name="stub",
        parser_provider="stub",
        parser_version="0",
        pages=(ParsedPage(page_number=1, width=612, height=792),),
        elements=(
            ParsedElement(
                reference="#/texts/0",
                element_type=ElementType.PARAGRAPH,
                reading_order=0,
                ordinal=0,
                depth=1,
                page_number=1,
                text="expected fragment",
            ),
        ),
        raw_artifact=b"{}",
        source_page_count=1,
    )
    result = evaluate_case(case, document)
    assert not result.passed
    assert any("tables 0 != 1" in failure for failure in result.failures)
    assert any("table 0 is missing" in failure for failure in result.failures)
