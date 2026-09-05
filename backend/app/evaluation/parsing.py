"""Parsing extraction-fidelity evaluation.

This measures whether the parser recovered the structure a human can see in the source document.
It is deliberately *not* RAG evaluation: there is no retrieval, no answer, no judge model and no
notion of medical correctness here. A document can score perfectly on extraction fidelity and
still be useless evidence; the two questions are kept separate on purpose.

Expectations are exact counts and required text fragments over synthetic fixtures whose structure
is known by construction, so the result is deterministic and reproducible.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ingestion.normalizer.text import normalize_text
from app.ingestion.parser.model import ParsedDocument

GOLD_SCHEMA = "medrag.parse.gold/1"


@dataclass(frozen=True)
class GoldCase:
    """One fixture and the structure it is known to contain."""

    name: str
    document: str
    description: str
    pages: int
    element_types: dict[str, int] = field(default_factory=dict)
    min_element_types: dict[str, int] = field(default_factory=dict)
    tables: int = 0
    figures: int = 0
    formulas: int = 0
    ocr_pages: int = 0
    figures_with_image: int = 0
    captioned_tables: int = 0
    captioned_figures: int = 0
    table_grid: list[list[int]] = field(default_factory=list)
    required_text: list[str] = field(default_factory=list)
    ordered_text: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CaseResult:
    name: str
    checks: int
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class Report:
    results: tuple[CaseResult, ...]

    @property
    def cases(self) -> int:
        return len(self.results)

    @property
    def passed_cases(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def checks(self) -> int:
        return sum(result.checks for result in self.results)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"{result.name}: {failure}" for result in self.results for failure in result.failures
        )

    def render(self) -> str:
        lines = [
            "Parsing extraction-fidelity evaluation",
            f"cases: {self.passed_cases}/{self.cases} passed",
            f"checks: {self.checks - len(self.failures)}/{self.checks} passed",
        ]
        lines += [f"  FAIL {failure}" for failure in self.failures]
        return "\n".join(lines)


def load_gold(path: Path) -> tuple[GoldCase, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != GOLD_SCHEMA:
        raise ValueError(f"Unsupported gold dataset schema: {payload.get('schema')!r}")
    return tuple(GoldCase(**case) for case in payload["cases"])


def _texts(parsed: ParsedDocument) -> str:
    return "\n".join(normalize_text(element.text or "") for element in parsed.elements)


def evaluate_case(case: GoldCase, parsed: ParsedDocument) -> CaseResult:
    failures: list[str] = []
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(message)

    check(len(parsed.pages) == case.pages, f"pages {len(parsed.pages)} != {case.pages}")
    check(
        [page.page_number for page in parsed.pages] == list(range(1, len(parsed.pages) + 1)),
        "page numbers are not the contiguous 1-based printed sequence",
    )
    check(
        [element.reading_order for element in parsed.elements] == list(range(len(parsed.elements))),
        "reading order is not a contiguous sequence",
    )

    counts: dict[str, int] = {}
    for element in parsed.elements:
        counts[element.element_type.value] = counts.get(element.element_type.value, 0) + 1
    for name, expected in case.element_types.items():
        check(counts.get(name, 0) == expected, f"{name} {counts.get(name, 0)} != {expected}")
    for name, minimum in case.min_element_types.items():
        check(counts.get(name, 0) >= minimum, f"{name} {counts.get(name, 0)} < {minimum}")

    tables = [element for element in parsed.elements if element.table is not None]
    figures = [element for element in parsed.elements if element.figure is not None]
    formulas = [element for element in parsed.elements if element.formula is not None]
    check(len(tables) == case.tables, f"tables {len(tables)} != {case.tables}")
    check(len(figures) == case.figures, f"figures {len(figures)} != {case.figures}")
    check(len(formulas) == case.formulas, f"formulas {len(formulas)} != {case.formulas}")
    check(
        len(parsed.ocr_pages) == case.ocr_pages,
        f"ocr pages {len(parsed.ocr_pages)} != {case.ocr_pages}",
    )
    check(
        sum(1 for element in figures if element.figure and element.figure.image)
        == (case.figures_with_image),
        "extracted figure images do not match the expected count",
    )
    check(
        sum(1 for element in tables if element.caption_references) == case.captioned_tables,
        "table caption relations do not match the expected count",
    )
    check(
        sum(1 for element in figures if element.caption_references) == case.captioned_figures,
        "figure caption relations do not match the expected count",
    )

    for index, (rows, columns, cells) in enumerate(case.table_grid):
        if index >= len(tables) or tables[index].table is None:
            check(False, f"table {index} is missing")
            continue
        table = tables[index].table
        assert table is not None
        check(table.row_count == rows, f"table {index} rows {table.row_count} != {rows}")
        check(
            table.column_count == columns,
            f"table {index} columns {table.column_count} != {columns}",
        )
        check(len(table.cells) == cells, f"table {index} cells {len(table.cells)} != {cells}")

    body = _texts(parsed)
    for fragment in case.required_text:
        check(fragment in body, f"missing required text {fragment!r}")
    positions = [body.find(fragment) for fragment in case.ordered_text]
    check(
        all(position >= 0 for position in positions) and positions == sorted(positions),
        "required fragments do not appear in the expected reading order",
    )

    check(
        all(element.bbox is None or element.bbox.valid for element in parsed.elements),
        "an element carries a malformed bounding box",
    )
    return CaseResult(case.name, checks, tuple(failures))


def evaluate(cases: tuple[GoldCase, ...], parse: Any) -> Report:
    """`parse` maps a gold case's document filename to a ParsedDocument."""
    return Report(tuple(evaluate_case(case, parse(case.document)) for case in cases))
