"""A page is only "lost" when its text is not in the parsed document at all.

The character rule could not tell loss from cross-page anchoring: a paragraph beginning on page 516
and ending on 517 is anchored to 516, which leaves 517 looking empty while nothing is missing. On
the real 932-page textbook that rule produced two ERRORs and **both** were false — 517 is the
cross-page case, and page 44 is a section divider whose decorative "S E C T I O N" lettering does
not survive tokenization while its title does. Measured against the stored parse and the original
PDF, both score 1.000 recovery.

What must not change: text that genuinely is not in the parse is still an ERROR, and an unmeasured
page is still an ERROR. See ADR-025.
"""

import pytest
from app.core.parsing_config import ParseThresholds
from app.ingestion.validation.parse_quality import (
    PAGE_CONTENT_ANCHORED_ELSEWHERE,
    PAGE_CONTENT_LOST,
    PAGE_EMPTY,
    PAGE_TEXT_BELOW_FLOOR,
    ElementFacts,
    PageFacts,
    ParseFacts,
    validate,
)
from app.ingestion.validation.text_recovery import PageRecovery, coverage, material_words, recovery
from app.models.enums import ElementType, ParseResult, Severity

THRESHOLDS = ParseThresholds()


def page(
    number: int = 1,
    *,
    text_chars: int = 5000,
    source_text_chars: int = 5000,
    found: PageRecovery | None = None,
) -> PageFacts:
    return PageFacts(
        page_number=number,
        width=612.0,
        height=792.0,
        text_chars=text_chars,
        element_count=10,
        source_text_chars=source_text_chars,
        ocr_used=False,
        has_preview=True,
        source_text_recovery=found,
    )


def element(number: int = 1, order: int = 0) -> ElementFacts:
    return ElementFacts(
        reference=f"#/texts/{number}",
        element_type=ElementType.PARAGRAPH,
        reading_order=order,
        page_number=number,
        has_bbox=True,
        bbox_valid=True,
        bbox_in_page=True,
        is_container=False,
    )


def facts(*pages: PageFacts) -> ParseFacts:
    return ParseFacts(
        source_page_count=len(pages),
        previews_requested=False,
        figures_requested=False,
        parser_warnings=(),
        pages=pages,
        elements=tuple(element(p.page_number, i) for i, p in enumerate(pages)),
        tables=(),
        figures=(),
        formulas=(),
    )


def finding_for(outcome, number: int):
    return next(f for f in outcome.findings if f.scope == "page" and f.page_number == number)


# ------------------------------------------------------------------ the measure itself


def test_material_words_ignores_single_letters_and_case():
    assert material_words("S E C T I O N 3 Basic Concepts") == {"basic", "concepts"}


def test_coverage_of_identical_text_is_total():
    source = "Passive immunization provides antibody."
    assert coverage(source, "passive immunization provides antibody") == 1.0


def test_coverage_is_insensitive_to_reflowed_whitespace_and_order():
    source = "large airborne particles\nare caught in the mucus"
    parsed = "the  mucus   caught large particles airborne are in"
    assert coverage(source, parsed) == 1.0


def test_coverage_reports_what_is_actually_absent():
    assert coverage("alpha beta gamma delta", "alpha beta") == 0.5


def test_a_source_with_no_material_words_is_covered_by_definition():
    assert coverage("", "anything") == 1.0
    assert coverage("S E C T I O N", "") == 1.0


def test_decorative_letter_spacing_does_not_count_as_missing():
    """The real page 44: display lettering plus a title the parser did keep."""
    source = "S E C T I O N \n3\nBASIC CONCEPTS IN THE  \nIMMUNE RESPONSE"
    assert coverage(source, "BASIC CONCEPTS IN THE IMMUNE RESPONSE") == 1.0


def test_recovery_measures_nothing_when_no_page_is_suspect(tmp_path):
    assert recovery(tmp_path / "absent.pdf", set(), {1: "text"}) == {}


def test_recovery_of_an_unreadable_file_excuses_nothing(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    assert recovery(broken, {1}, {1: "text"}) == {}


# ------------------------------------------------------------------- what the validator does


def test_text_present_on_the_page_itself_is_not_lost():
    """Below the floor, but every material word is there: furniture, not loss."""
    found = PageRecovery(own=1.0, neighbourhood=1.0, pages=(1, 2))
    outcome = validate(facts(page(1, text_chars=37, source_text_chars=56, found=found)), THRESHOLDS)
    finding = finding_for(outcome, 1)
    assert finding.code == PAGE_TEXT_BELOW_FLOOR
    assert finding.severity is Severity.WARNING


def test_text_anchored_to_an_adjacent_page_is_not_lost():
    """The real page 517: absent from its own page, wholly present next door."""
    found = PageRecovery(own=0.046, neighbourhood=1.0, pages=(516, 517, 518))
    outcome = validate(
        facts(page(517, text_chars=28, source_text_chars=815, found=found)), THRESHOLDS
    )
    finding = finding_for(outcome, 517)
    assert finding.code == PAGE_CONTENT_ANCHORED_ELSEWHERE
    assert finding.severity is Severity.WARNING
    assert finding.details["compared_pages"] == [516, 517, 518]
    assert finding.details["neighbourhood_recovery"] == 1.0


def test_text_that_is_genuinely_absent_is_still_an_error():
    """The condition this validator exists for is unchanged."""
    found = PageRecovery(own=0.1, neighbourhood=0.35, pages=(9, 10, 11))
    outcome = validate(
        facts(page(10, text_chars=12, source_text_chars=4000, found=found)), THRESHOLDS
    )
    finding = finding_for(outcome, 10)
    assert finding.code == PAGE_CONTENT_LOST
    assert finding.severity is Severity.ERROR
    assert outcome.blocking


def test_partial_recovery_below_the_bar_is_still_loss():
    """Half a page present is half a page missing."""
    found = PageRecovery(own=0.5, neighbourhood=0.9, pages=(4, 5, 6))
    outcome = validate(
        facts(page(5, text_chars=20, source_text_chars=3000, found=found)), THRESHOLDS
    )
    assert finding_for(outcome, 5).code == PAGE_CONTENT_LOST


def test_an_unmeasured_page_fails_closed():
    """No measurement is not an excuse; it is the absence of one."""
    outcome = validate(
        facts(page(3, text_chars=10, source_text_chars=4000, found=None)), THRESHOLDS
    )
    finding = finding_for(outcome, 3)
    assert finding.code == PAGE_CONTENT_LOST
    assert finding.severity is Severity.ERROR


def test_a_page_with_no_source_text_is_still_merely_empty():
    """Nothing to lose: a blank page is not a parser failure and never was."""
    outcome = validate(facts(page(2, text_chars=0, source_text_chars=0)), THRESHOLDS)
    assert finding_for(outcome, 2).code == PAGE_EMPTY
    assert finding_for(outcome, 2).severity is Severity.WARNING


def test_a_healthy_page_raises_nothing():
    outcome = validate(facts(page(1)), THRESHOLDS)
    assert not [f for f in outcome.findings if f.scope == "page"]


def test_the_findings_carry_enough_provenance_to_investigate():
    found = PageRecovery(own=0.0, neighbourhood=1.0, pages=(516, 517, 518))
    outcome = validate(
        facts(page(517, text_chars=28, source_text_chars=815, found=found)), THRESHOLDS
    )
    details = finding_for(outcome, 517).details
    assert details["parsed_chars"] == 28 and details["source_text_chars"] == 815
    assert details["own_page_recovery"] == 0.0
    assert details["recovery_required"] == THRESHOLDS.min_page_text_recovery
    assert finding_for(outcome, 517).page_number == 517


@pytest.mark.parametrize("ratio", [0.0, 0.5, 0.97])
def test_the_bar_is_essentially_everything(ratio):
    found = PageRecovery(own=ratio, neighbourhood=ratio, pages=(1,))
    outcome = validate(
        facts(page(1, text_chars=10, source_text_chars=900, found=found)), THRESHOLDS
    )
    assert finding_for(outcome, 1).code == PAGE_CONTENT_LOST


def test_recovery_is_deterministic():
    found = PageRecovery(own=0.2, neighbourhood=1.0, pages=(1, 2))
    results = {
        validate(facts(page(2, text_chars=10, source_text_chars=900, found=found)), THRESHOLDS)
        .findings[0]
        .code
        for _ in range(10)
    }
    assert results == {PAGE_CONTENT_ANCHORED_ELSEWHERE}


def test_a_document_whose_only_page_findings_are_recovered_is_not_blocking():
    """Both real ERRORs were false; a document like that must not be forced into review."""
    recovered = PageRecovery(own=1.0, neighbourhood=1.0, pages=(1, 2))
    anchored = PageRecovery(own=0.0, neighbourhood=1.0, pages=(1, 2, 3))
    # A realistic shape: a long document with two suspect pages. A three-page document with two
    # near-empty pages would fail the non-empty-page ratio on its own, which is correct and is a
    # different rule.
    healthy = [page(n) for n in range(3, 23)]
    outcome = validate(
        facts(
            page(1, text_chars=37, source_text_chars=56, found=recovered),
            page(2, text_chars=28, source_text_chars=815, found=anchored),
            *healthy,
        ),
        THRESHOLDS,
    )
    assert not any(f.severity is Severity.ERROR for f in outcome.findings)
    assert outcome.result is not ParseResult.FAIL
