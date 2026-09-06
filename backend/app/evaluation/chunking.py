"""Chunk construction evaluation over synthetic normalized parse datasets.

This measures whether the chunker preserved the structure that was present in its input: what it
kept together, what it kept apart, what it carried forward verbatim and what it never invented.

It is not retrieval evaluation and not RAG evaluation. There is no query, no index, no ranking and
no Recall@K here, because nothing is retrievable yet; those numbers become meaningful only once
embeddings and an index exist. It is also not a medical accuracy measure: the fixtures are
synthetic structures with plausible-looking but invented content, so a perfect score says the
chunker is faithful to its input, never that the input is clinically correct.

Every input is a frozen normalized dataset with fixed identifiers, so the whole harness runs
offline: no PDF is parsed, no model weight is downloaded and only the bundled pinned tokenizer is
used for measurement.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.chunking_config import ChunkingConfig
from app.ingestion.chunking.builder import Builder
from app.ingestion.chunking.model import ChunkDataset, ChunkInput
from app.ingestion.chunking.quality import validate
from app.ingestion.chunking.tokenizer import TokenCounter

GOLD_SCHEMA = "chunk-gold-m3-v1"


@dataclass(frozen=True)
class GoldCase:
    """One normalized fixture and the chunk structure it is known to imply."""

    id: str
    source: ChunkInput
    types: dict[str, int] = field(default_factory=dict)
    required_text: list[str] = field(default_factory=list)
    forbidden_text: list[str] = field(default_factory=list)
    ordered_fragments: list[str] = field(default_factory=list)
    separate_elements: list[list[int]] = field(default_factory=list)
    question_count: int | None = None
    answers: list[str | None] | None = None
    options_atomic: bool = False
    table_headers: bool = False
    separate_artifacts: bool = False
    cross_page: bool = False
    excluded: int | None = None
    all_source_mapped: bool = False


@dataclass(frozen=True)
class CaseResult:
    name: str
    checks: int
    failures: tuple[str, ...]
    chunks: int
    dataset_hash: str

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
            "Chunk construction evaluation (not retrieval or medical accuracy)",
            f"cases: {self.passed_cases}/{self.cases} passed",
            f"checks: {self.checks - len(self.failures)}/{self.checks} passed",
        ]
        lines += [
            f"  {result.name}: {result.chunks} chunks, dataset {result.dataset_hash[:12]}"
            for result in self.results
        ]
        lines += [f"  FAIL {failure}" for failure in self.failures]
        return "\n".join(lines)


def load_gold(path: Path) -> tuple[GoldCase, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != GOLD_SCHEMA:
        raise ValueError(f"Unsupported gold dataset schema: {payload.get('schema')!r}")
    return tuple(
        GoldCase(
            id=case["id"],
            source=ChunkInput.model_validate(case["source"]),
            **case.get("expected", {}),
        )
        for case in payload["cases"]
    )


def evaluate_case(
    case: GoldCase, config: ChunkingConfig, tokens: TokenCounter
) -> tuple[CaseResult, ChunkDataset]:
    dataset = Builder(case.source, config, tokens).build()
    chunks = dataset.chunks
    failures: list[str] = []
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(message)

    for kind, minimum in case.types.items():
        found = sum(1 for chunk in chunks if chunk.kind == kind)
        check(found >= minimum, f"{kind} chunks {found} < {minimum}")

    body = "\n".join(chunk.source_text for chunk in chunks)
    for fragment in case.required_text:
        check(fragment in body, f"missing required source text {fragment!r}")
    for fragment in case.forbidden_text:
        check(fragment not in body, f"excluded text {fragment!r} was carried into a chunk")

    if case.ordered_fragments:
        children = "\n".join(chunk.source_text for chunk in chunks if chunk.kind == "TEXT_CHILD")
        positions = [children.find(fragment) for fragment in case.ordered_fragments]
        check(
            all(position >= 0 for position in positions) and positions == sorted(positions),
            "child chunks do not follow source reading order",
        )

    for first, second in case.separate_elements:
        identities = {case.source.elements[first].id, case.source.elements[second].id}
        check(
            not any(
                identities <= {span.element_id for span in chunk.spans}
                for chunk in chunks
                if chunk.kind == "TEXT_CHILD"
            ),
            f"elements {first} and {second} were merged across a structural boundary",
        )

    if case.question_count is not None:
        check(
            len(dataset.questions) == case.question_count,
            f"questions {len(dataset.questions)} != {case.question_count}",
        )
    if case.answers is not None:
        answers = [question.explicit_answer for question in dataset.questions]
        check(answers == case.answers, f"explicit source answers {answers} != {case.answers}")
        check(
            not any(question.answer_inferred for question in dataset.questions),
            "an answer was inferred rather than read from the source",
        )
    if case.options_atomic:
        for question in dataset.questions:
            owning = [
                chunk
                for chunk in chunks
                if chunk.question_key == question.key and chunk.kind == "QUESTION"
            ]
            check(len(owning) == 1, "a question is not carried by exactly one chunk")
            check(
                bool(owning)
                and all(option.text in owning[0].source_text for option in question.options),
                "question options were separated from their question",
            )
    if case.table_headers:
        parts = [chunk for chunk in chunks if chunk.kind == "TABLE_PART"]
        check(bool(parts), "the table was not split into parts")
        check(
            all(part.metadata.get("headers") for part in parts),
            "a table part lost its repeated header rows",
        )
        check(
            len({part.metadata.get("headers") for part in parts}) <= 1,
            "repeated table headers are not identical across parts",
        )
    if case.separate_artifacts:
        check(
            all(len(chunk.artifact_ids) == 1 for chunk in chunks if chunk.kind == "TABLE"),
            "table chunks do not each reference exactly one source artifact",
        )
    if case.cross_page:
        check(
            any(
                chunk.page_start < chunk.page_end
                for chunk in chunks
                if chunk.page_start and chunk.page_end
            ),
            "no chunk spans the page transition present in the source",
        )
    if case.excluded is not None:
        check(
            len(dataset.excluded_element_ids) == case.excluded,
            f"excluded elements {len(dataset.excluded_element_ids)} != {case.excluded}",
        )

    result, _, metrics = validate(case.source, dataset, config, tokens)
    check(
        result in {"PASS", "PASS_WITH_WARNINGS"},
        f"chunk validation result is {result}",
    )
    if case.all_source_mapped:
        check(
            metrics["missing_provenance"] == 0 and metrics["omitted_characters"] == 0,
            "source text was omitted: "
            f"{metrics['missing_provenance']} unmapped elements, "
            f"{metrics['omitted_characters']} characters",
        )
        check(
            metrics["split_omitted_characters"] == 0,
            "splitting dropped source text from the retrieval units",
        )

    # Determinism is a property of the chunker, so it is measured here rather than assumed.
    repeat = Builder(case.source, config, tokens).build()
    check(
        [chunk.key for chunk in repeat.chunks] == [chunk.key for chunk in chunks],
        "identical input produced different chunk identities",
    )
    check(
        repeat.input_fingerprint == dataset.input_fingerprint,
        "identical input produced a different input fingerprint",
    )

    return (
        CaseResult(case.id, checks, tuple(failures), len(chunks), str(metrics["dataset_hash"])),
        dataset,
    )


def evaluate(cases: tuple[GoldCase, ...], config: ChunkingConfig, tokens: TokenCounter) -> Report:
    return Report(tuple(evaluate_case(case, config, tokens)[0] for case in cases))


def describe(report: Report) -> dict[str, Any]:
    return {
        "cases": report.cases,
        "passed_cases": report.passed_cases,
        "checks": report.checks,
        "failures": list(report.failures),
    }
