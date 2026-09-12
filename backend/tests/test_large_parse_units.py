"""Post-M12 large-book parsing: bounded memory through page windows, provenance unchanged.

The measurement that motivated this lives in `.local/measure_parse_memory.py`: a single
whole-document Docling conversion retained ~18 MiB per page and grew linearly (200 pages ->
3663 MiB), while windowed conversion plateaued (200 pages -> 1389 MiB, 400 pages -> 1571 MiB).
These tests protect the properties that make that safe, not the megabyte figures, which are
host-dependent.

No large fixture is committed; window planning and joining are exercised directly.
"""

from dataclasses import replace
from pathlib import Path

import pytest
from app.core.parsing_config import ParsingConfig
from app.ingestion.parser.docling_adapter import _requalify, _source_text_chars, _windows
from app.ingestion.parser.model import (
    ParsedElement,
    ParserConfig,
    document_budget_seconds,
)
from app.models.enums import ElementType
from pydantic import ValidationError

BASE = dict(
    ocr_mode="AUTO",
    extract_tables=True,
    extract_formulas=True,
    extract_figures=True,
    generate_page_previews=True,
    preview_scale=1.5,
    timeout_seconds=900,
    max_pages=2000,
)


def config(**overrides: object) -> ParserConfig:
    return ParserConfig(**{**BASE, **overrides})  # type: ignore[arg-type]


def element(reference: str = "#/texts/0", **overrides: object) -> ParsedElement:
    defaults = dict(
        reference=reference,
        element_type=ElementType.PARAGRAPH,
        reading_order=0,
        ordinal=0,
        depth=1,
        parent_reference="#/body",
        page_number=1,
        text="text",
        bbox=None,
        source_label="text",
        content_layer=None,
        caption_of=None,
        caption_references=(),
        table=None,
        figure=None,
        formula=None,
        metadata={},
    )
    return ParsedElement(**{**defaults, **overrides})  # type: ignore[arg-type]


# ------------------------------------------------------------------------- window planning


def test_short_documents_keep_the_single_call_path():
    """Existing fixtures must parse exactly as before, or M2's determinism claim is broken."""
    windowed = config(page_window_size=25, page_window_threshold=50)
    assert _windows(1, windowed) is None
    assert _windows(30, windowed) is None
    assert _windows(50, windowed) is None, "the threshold is inclusive"


def test_long_documents_are_converted_in_windows():
    windowed = config(page_window_size=25, page_window_threshold=50)
    plan = _windows(120, windowed)
    assert plan == ((1, 25), (26, 50), (51, 75), (76, 100), (101, 120))


def test_windows_cover_every_page_exactly_once():
    windowed = config(page_window_size=25, page_window_threshold=50)
    for pages in (51, 99, 100, 101, 340, 400, 1001):
        plan = _windows(pages, windowed)
        assert plan is not None
        covered = [page for first, last in plan for page in range(first, last + 1)]
        assert covered == list(range(1, pages + 1)), f"{pages} pages"


def test_windowing_can_be_disabled_entirely():
    assert _windows(5000, config(page_window_size=0)) is None


def test_a_window_larger_than_the_threshold_still_converts_in_one_call():
    """Otherwise the first window would exceed the document that triggered windowing."""
    assert _windows(80, config(page_window_size=100, page_window_threshold=50)) is None


def test_the_policy_refuses_an_incoherent_window_configuration():
    with pytest.raises(ValidationError):
        ParsingConfig(page_window_size=100, page_window_threshold=50)


def test_the_shipped_policy_windows_a_real_textbook_but_not_a_fixture():
    policy = ParsingConfig()
    windowed = config(
        page_window_size=policy.page_window_size,
        page_window_threshold=policy.page_window_threshold,
    )
    assert _windows(10, windowed) is None, "fixtures stay on the single-call path"
    plan = _windows(340, windowed)
    assert plan is not None and len(plan) > 1, "a 340-page textbook is windowed"


# ------------------------------------------------------------------- provenance across windows


def test_absolute_page_numbers_are_never_rewritten():
    """Docling reports absolute page numbers under `page_range`, and joining must not touch them.

    Page 101 parsed in the window 101-125 must remain page 101, not local page 1.
    """
    for window in range(5):
        assert _requalify(element(page_number=101), window, 0).page_number == 101


def test_references_are_namespaced_so_windows_cannot_collide():
    """Every window restarts Docling's `#/texts/N` numbering.

    Joined naively, the second window's `#/texts/0` would be the same key as the first's, and a
    parent lookup would silently re-parent an element into a different part of the book.
    """
    first = _requalify(element("#/texts/0"), 0, 0)
    second = _requalify(element("#/texts/0"), 1, 1)
    assert first.reference != second.reference
    assert first.reference == "w0#/texts/0" and second.reference == "w1#/texts/0"


def test_relationships_stay_inside_their_own_window():
    joined = _requalify(
        element(
            "#/texts/4",
            parent_reference="#/groups/1",
            caption_of="#/pictures/2",
            caption_references=("#/texts/9", "#/texts/10"),
        ),
        3,
        77,
    )
    assert joined.parent_reference == "w3#/groups/1"
    assert joined.caption_of == "w3#/pictures/2"
    assert joined.caption_references == ("w3#/texts/9", "w3#/texts/10")


def test_reading_order_is_continuous_across_the_join():
    """Order is assigned by the join, not carried from each window's own counter."""
    joined = [_requalify(element(f"#/texts/{index % 3}"), index // 3, index) for index in range(9)]
    assert [e.reading_order for e in joined] == list(range(9))


def test_joining_leaves_no_dangling_parent_reference():
    window_elements = [
        element("#/body", parent_reference=None),
        element("#/texts/0", parent_reference="#/body"),
        element("#/texts/1", parent_reference="#/body"),
    ]
    joined = []
    order = 0
    for window in range(3):
        for item in window_elements:
            joined.append(_requalify(item, window, order))
            order += 1
    references = {e.reference for e in joined}
    parents = {e.parent_reference for e in joined if e.parent_reference}
    assert parents <= references, "every parent must resolve within the joined document"
    assert len(references) == len(joined), "references must stay unique after joining"


def test_nothing_else_about_an_element_is_altered_by_joining():
    """Requalification touches identity and order only; content and geometry are untouched."""
    original = element("#/texts/7", text="Dose is 40 mg once daily.", page_number=203)
    joined = _requalify(original, 8, 999)
    assert joined.text == original.text
    assert joined.element_type == original.element_type
    assert joined.bbox == original.bbox
    assert joined.ordinal == original.ordinal and joined.depth == original.depth
    assert joined.source_label == original.source_label
    # Everything except the five identity/order fields is byte-identical.
    assert (
        replace(
            joined,
            reference=original.reference,
            parent_reference=original.parent_reference,
            caption_of=original.caption_of,
            caption_references=original.caption_references,
            reading_order=original.reading_order,
        )
        == original
    )


# ------------------------------------------------------------------ whole-document time budget


def test_the_document_budget_scales_with_length():
    """One 25-page window measured ~143 s on real textbook content, so a 932-page book is well
    over an hour of work. A constant budget cannot serve both it and a one-page leaflet: the
    value that lets the book finish would let a stuck leaflet hold the worker for hours."""
    policy = ParsingConfig()
    assert policy.document_timeout_for(1) < policy.document_timeout_for(100)
    assert policy.document_timeout_for(100) < policy.document_timeout_for(932)


def test_the_document_budget_covers_a_real_932_page_textbook():
    """Measured at ~5.7 s/page on this host; the budget must not fail a healthy long parse."""
    policy = ParsingConfig()
    assert policy.document_timeout_for(932) > 932 * 5.7


def test_the_document_budget_is_capped_however_long_the_book_is():
    policy = ParsingConfig()
    assert policy.document_timeout_for(policy.max_pages) == policy.max_document_timeout_seconds


def test_the_task_limits_clear_the_document_budget_and_one_more_window():
    """The budget is checked between windows, so a parse already over budget still finishes the
    window it is in. If the soft task limit did not clear that overshoot the worker would kill
    the task first and the diagnosable PARSER_TIMEOUT would be lost."""
    policy = ParsingConfig()
    overshoot = policy.max_document_timeout_seconds + policy.timeout_seconds
    assert policy.task_soft_timeout_seconds > overshoot
    assert policy.task_timeout_seconds > policy.task_soft_timeout_seconds


def test_policy_and_adapter_compute_the_same_budget():
    """The formula lives in one place precisely so enforcement cannot drift from policy."""
    policy = ParsingConfig()
    for pages in (1, 25, 26, 100, 340, 932, 2000):
        assert policy.document_timeout_for(pages) == document_budget_seconds(
            pages,
            policy.timeout_seconds,
            policy.document_seconds_per_page,
            policy.max_document_timeout_seconds,
        )


def test_the_budget_can_be_switched_off():
    assert document_budget_seconds(932, 900, 0.0, 14400) == 0
    assert document_budget_seconds(932, 900, 12.0, 0) == 0


def test_the_adapter_checks_the_budget_between_windows():
    """A conversion call is opaque, so the seams are the only place the whole-document budget
    can be enforced. Each window remains separately bounded by Docling's own per-call timeout."""
    from pathlib import Path

    source = Path("backend/app/ingestion/parser/docling_adapter.py").read_text(encoding="utf-8")
    assert "budget = document_budget_seconds(" in source
    assert "raise ParserError(PARSER_TIMEOUT" in source


# ------------------------------------------------------------ bounded text-layer extraction


def test_the_text_layer_can_be_read_one_window_at_a_time():
    """Reading a whole book's text layer up front is not bounded-memory.

    pypdf caches every page's decompressed content stream as it goes; on the 932-page textbook
    that transient cost measured ~1.3 GiB, paid before Docling had converted a single page and
    never returned to the allocator. Restricting extraction to the window that needs it keeps
    the cost bounded by the same window size as conversion.
    """
    fixture = Path("backend/tests/fixtures/parsing/multi-page.pdf")
    whole = _source_text_chars(fixture)
    assert sorted(whole) == [1, 2, 3]

    window = _source_text_chars(fixture, (2, 3))
    assert sorted(window) == [2, 3], "only the requested pages are read"
    assert window[2] == whole[2] and window[3] == whole[3], "and the counts are identical"


def test_a_window_past_the_end_of_the_document_is_clamped():
    fixture = Path("backend/tests/fixtures/parsing/multi-page.pdf")
    assert sorted(_source_text_chars(fixture, (3, 99))) == [3]


def test_the_windowed_path_reads_the_text_layer_per_window():
    source = Path("backend/app/ingestion/parser/docling_adapter.py").read_text(encoding="utf-8")
    assert "text_layer = _source_text_chars(source.path, (first, last))" in source


# --------------------------------------------------------------------- allocator retention


def test_each_window_returns_its_freed_pages_to_the_operating_system():
    """Freeing the objects is not the same as releasing the memory.

    With windowing and per-window text extraction in place, worker RSS still ratcheted ~6 MiB per
    page across a 932-page book while the parse output actually retained grew ~0.2 MiB per page:
    glibc was keeping the freed arenas rather than reusing them. Trimming between windows held
    settled RSS flat (1248 MiB after the first window, 1321 MiB after the eighth) where it had
    climbed from 2267 MiB to 2842 MiB over the same eight windows.
    """
    source = Path("backend/app/ingestion/parser/docling_adapter.py").read_text(encoding="utf-8")
    body = source.split("def _convert_windowed(", 1)[1].split("\n    def ", 1)[0]
    assert "gc.collect()" in body and "_release_heap()" in body


def test_trimming_is_a_no_op_where_the_facility_does_not_exist():
    """The parse must be correct on any platform; only its memory profile differs."""
    from app.ingestion.parser.docling_adapter import _release_heap

    _release_heap()  # must not raise on this host, whatever it is


def test_the_worker_caps_its_allocator_arenas():
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    worker = compose.split("\n  worker:", 1)[1].split("\n  retrieval:", 1)[0]
    assert "MALLOC_ARENA_MAX" in worker


# ----------------------------------------------------------------------------- lease safety


def test_the_parser_protocol_carries_a_progress_callback():
    """A long conversion must be able to report advancement so the caller can renew its lease.

    Without it the lease is renewed only at stage boundaries, so a worker killed mid-book leaves
    the job showing PARSING until the whole lease elapses.
    """
    import inspect

    from app.ingestion.parser.docling_adapter import DoclingDocumentParser
    from app.ingestion.parser.model import DocumentParser

    assert "on_progress" in inspect.signature(DocumentParser.parse).parameters
    assert "on_progress" in inspect.signature(DoclingDocumentParser.parse).parameters


def test_the_parse_service_renews_the_lease_between_windows():
    from pathlib import Path

    source = Path("backend/app/services/parsing.py").read_text(encoding="utf-8")
    assert "on_progress=renew" in source
    assert "self._beat(beating)" in source


def test_the_lease_is_sized_for_liveness_not_for_the_whole_document():
    """Heartbeating is what makes this possible.

    Without beats the lease would have to outlive the entire parse, so a 932-page book would
    need a lease of hours and a dead worker would hold the job in PARSING for just as long. With
    beats between windows the lease only has to outlive one conversion call — and it must, since
    nothing can renew it from inside one.
    """
    policy = ParsingConfig()
    assert policy.lease_seconds > policy.timeout_seconds
    assert policy.lease_seconds < policy.document_timeout_for(932)

    with pytest.raises(ValidationError):
        ParsingConfig(lease_seconds=policy.timeout_seconds)
