"""Measured chunk integrity and quality. These are not retrieval accuracy metrics."""

from collections import Counter, defaultdict
from typing import Any
from uuid import UUID

from app.core.chunking_config import ChunkingConfig
from app.ingestion.chunking.builder import digest
from app.ingestion.chunking.model import ChunkDataset, ChunkInput, Finding, SourceElement
from app.ingestion.chunking.tokenizer import TokenCounter


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def omitted_characters(element: SourceElement, intervals: list[tuple[int, int]]) -> int:
    """Non-whitespace source characters that no chunk span covers.

    A split paragraph maps to several spans, so it is not enough that the element identity
    appears somewhere: the spans must between them cover the element's meaningful text.
    Whitespace between spans is not source loss; a dropped word or trailing sentence is.
    """
    covered = _merge([(max(0, s), min(len(element.text), e)) for s, e in intervals if e > s])
    position = 0
    omitted = 0
    for start, end in covered + [(len(element.text), len(element.text))]:
        omitted += sum(1 for character in element.text[position:start] if not character.isspace())
        position = max(position, end)
    return omitted


def validate(
    source: ChunkInput, dataset: ChunkDataset, config: ChunkingConfig, tokens: TokenCounter
) -> tuple[str, list[Finding], dict[str, Any]]:
    findings = list(dataset.findings)
    chunks = {c.key: c for c in dataset.chunks}
    elements = {e.id: e for e in source.elements}
    artifacts = {a.id: a for a in source.artifacts}

    def add(
        code: str,
        message: str,
        key: str | None = None,
        severity: str = "CRITICAL",
        details: dict[str, Any] | None = None,
    ) -> None:
        findings.append(
            Finding(
                severity=severity, code=code, message=message, chunk_key=key, details=details or {}
            )
        )

    if not chunks:
        add("CHUNK_EMPTY_DATASET", "The parse produced no usable chunk dataset.")
    if len(chunks) != len(dataset.chunks):
        add("CHUNK_HASH_COLLISION", "Two chunks have the same content identity.")
    if len({c.sequence for c in dataset.chunks}) != len(chunks):
        add("CHUNK_SEQUENCE_INVALID", "Chunk sequences are not unique.")
    tiny = oversized = cross_page = 0
    children = [c for c in chunks.values() if c.kind in {"TEXT_CHILD", "LIST"}]
    mapped = set()
    covered: dict[UUID, list[tuple[int, int]]] = defaultdict(list)
    # A TEXT_PARENT is the union of its children, so it would mask a fragment the split dropped.
    # Retrieval-unit coverage is therefore measured without it.
    retrieval_covered: dict[UUID, list[tuple[int, int]]] = defaultdict(list)
    for chunk in chunks.values():
        if not chunk.spans:
            add("CHUNK_SOURCE_MISSING", "A chunk has no source mapping.", chunk.key)
        pages = set()
        for span in chunk.spans:
            element = elements.get(span.element_id)
            if (
                element is None
                or span.start < 0
                or span.end < span.start
                or span.end > len(element.text)
            ):
                add(
                    "CHUNK_SOURCE_INVALID",
                    "A source mapping has invalid identity or offsets.",
                    chunk.key,
                )
                continue
            mapped.add(element.id)
            covered[element.id].append((span.start, span.end))
            if chunk.kind != "TEXT_PARENT":
                retrieval_covered[element.id].append((span.start, span.end))
            if element.page_number:
                pages.add(element.page_number)
        if not pages or (chunk.page_start, chunk.page_end) != (min(pages), max(pages)):
            add(
                "CHUNK_PAGE_RANGE_INVALID",
                "Chunk page range does not reconcile with its sources.",
                chunk.key,
            )
        if chunk.page_start and chunk.page_end and chunk.page_end > chunk.page_start:
            cross_page += 1
        if not chunk.source_text.strip() and chunk.kind != "FIGURE_CONTEXT":
            add("CHUNK_EMPTY", "A chunk has no source text.", chunk.key)
        if (
            tokens.count(chunk.source_text) != chunk.token_count
            or tokens.count(chunk.retrieval_text) != chunk.retrieval_token_count
        ):
            add(
                "CHUNK_TOKEN_COUNT_INVALID",
                "Token count does not match the pinned tokenizer.",
                chunk.key,
            )
        if any(i not in elements for i in chunk.hierarchy):
            add(
                "CHUNK_HIERARCHY_INVALID", "A hierarchy link does not resolve to source.", chunk.key
            )
        if chunk.parent_key:
            parent = chunks.get(chunk.parent_key)
            if (
                parent is None
                or parent.key == chunk.key
                or parent.kind not in {"TEXT_PARENT", "QUESTION"}
            ):
                add("CHUNK_PARENT_INVALID", "A child has an invalid parent.", chunk.key)
        elif chunk.kind in {"TEXT_CHILD", "LIST", "QUESTION_EXPLANATION"}:
            add("CHUNK_PARENT_MISSING", "A child is missing its parent.", chunk.key)
        for artifact in chunk.artifact_ids:
            if artifact not in artifacts:
                add(
                    "CHUNK_ARTIFACT_INVALID",
                    "An artifact relationship does not resolve.",
                    chunk.key,
                )
        if (
            chunk.kind in {"TABLE", "TABLE_PART", "FORMULA", "FIGURE_CONTEXT"}
            and not chunk.artifact_ids
        ):
            add("CHUNK_ARTIFACT_MISSING", "A structured chunk has no source artifact.", chunk.key)
        if chunk.kind == "TABLE_PART" and not chunk.metadata.get("headers"):
            add(
                "CHUNK_TABLE_HEADERS_MISSING",
                "A table part has no declared header context.",
                chunk.key,
                "ERROR",
            )
        if chunk.kind == "FORMULA" and not chunk.metadata.get("expression"):
            add("CHUNK_FORMULA_EMPTY", "A formula has no source expression.", chunk.key)
        if chunk.kind == "FIGURE_CONTEXT" and chunk.metadata.get("visual_only"):
            add(
                "CHUNK_FIGURE_NO_TEXT",
                "The figure has no source text; its image remains available.",
                chunk.key,
                "WARNING",
            )
        limit = (
            config.parent_target_tokens
            if chunk.kind == "TEXT_PARENT"
            else config.child_target_tokens
        )
        if chunk.kind in {"TABLE", "TABLE_PART"}:
            limit = config.table_max_tokens
        if chunk.kind == "QUESTION_EXPLANATION":
            limit = config.explanation_max_tokens
        # A retrieval-eligible chunk over the retrieval budget can never be embedded: the encoder
        # refuses to truncate, so the whole run would fail later with the cause two stages away.
        # Failing here names the chunk while rechunking under a different policy is still the
        # obvious remedy. TEXT_PARENT is exempt by design — a parent is a context container that
        # is never an embedding input, so its larger target is not a contradiction.
        #
        # Measured on the retrieval representation, because that is the string the encoder receives
        # as the input body. The source text is a lower bound on it: the hierarchy prefix, and a
        # figure's no-text placeholder, are part of what gets embedded but part of no source text.
        # Judging the source alone let a body over the budget be declared embeddable.
        unembeddable = (
            chunk.kind != "TEXT_PARENT"
            and chunk.retrieval_token_count > config.retrieval_budget_tokens
        )
        if unembeddable:
            oversized += 1
            add(
                "CHUNK_OVERSIZED",
                "A retrieval unit is too large to be embedded without truncation.",
                chunk.key,
                "ERROR",
                # The measurement the decision was made on, so a reviewer sees 396 / 384 rather
                # than having to recount: retrieval tokens of the embedded representation.
                {
                    "measured_tokens": chunk.retrieval_token_count,
                    "limit_tokens": config.retrieval_budget_tokens,
                    "measured_on": "retrieval_text",
                },
            )
        elif chunk.token_count > limit:
            oversized += 1
            add(
                "CHUNK_OVERSIZED",
                "An atomic source unit exceeds the configured target.",
                chunk.key,
                "WARNING",
                {
                    "measured_tokens": chunk.token_count,
                    "limit_tokens": limit,
                    "measured_on": "normalized_text",
                },
            )
        if chunk.token_count > config.thresholds.max_atomic_tokens and chunk.kind != "TEXT_PARENT":
            add(
                "CHUNK_ATOMIC_LIMIT", "An atomic unit exceeds the review limit.", chunk.key, "ERROR"
            )
        if (
            chunk.kind in {"TEXT_CHILD", "LIST"}
            and chunk.token_count < config.thresholds.tiny_tokens
        ):
            tiny += 1
            add(
                "CHUNK_TINY",
                "A structurally separate child is below the token target.",
                chunk.key,
                "WARNING",
            )
    option_splits = 0
    for question in dataset.questions:
        linked = [
            c for c in chunks.values() if c.question_key == question.key and c.kind == "QUESTION"
        ]
        if len(linked) != 1 or any(
            option.text not in linked[0].source_text for option in question.options
        ):
            option_splits += 1
            add("CHUNK_QUESTION_OPTIONS_SPLIT", "Question options do not remain with the question.")
        if question.answer_inferred:
            add(
                "CHUNK_ANSWER_INFERRED",
                "An answer was marked inferred rather than explicit source content.",
            )
    eligible = {e.id for e in elements.values() if e.text.strip()} - set(
        dataset.excluded_element_ids
    )
    missing = eligible - mapped
    if missing:
        findings.append(
            Finding(
                severity="CRITICAL",
                code="CHUNK_SOURCE_COVERAGE",
                message="Some source elements were omitted.",
                details={"count": len(missing)},
            )
        )
    # An element that is mapped is not thereby preserved: splitting must cover its text.
    omitted = {
        identity: count
        for identity in sorted(eligible & mapped, key=str)
        if (count := omitted_characters(elements[identity], covered[identity]))
    }
    if omitted:
        findings.append(
            Finding(
                severity="CRITICAL",
                code="CHUNK_SOURCE_TEXT_OMITTED",
                message="Source characters inside mapped elements are absent from every chunk.",
                details={"elements": len(omitted), "characters": sum(omitted.values())},
            )
        )
    split_omitted = {
        identity: count
        for identity in sorted(eligible & mapped, key=str)
        if (count := omitted_characters(elements[identity], retrieval_covered[identity]))
        and identity not in omitted
    }
    if split_omitted:
        findings.append(
            Finding(
                severity="ERROR",
                code="CHUNK_SPLIT_TEXT_OMITTED",
                message="Splitting dropped source text that only a parent chunk still carries.",
                details={
                    "elements": len(split_omitted),
                    "characters": sum(split_omitted.values()),
                },
            )
        )
    if len(children) >= 4 and tiny / len(children) > config.thresholds.max_tiny_ratio:
        add("CHUNK_TINY_RATIO", "Too many children are isolated small fragments.", severity="ERROR")
    if len(chunks) >= 4 and oversized / len(chunks) > config.thresholds.max_oversized_ratio:
        add("CHUNK_OVERSIZED_RATIO", "Too many chunks exceed configured targets.", severity="ERROR")
    counts = Counter(c.kind for c in chunks.values())
    metrics = {
        "chunks": len(chunks),
        "by_type": dict(sorted(counts.items())),
        "parents": counts["TEXT_PARENT"],
        "children": sum(counts[k] for k in ("TEXT_CHILD", "LIST")),
        "questions": len(dataset.questions),
        "tables": counts["TABLE"] + counts["TABLE_PART"],
        "table_parts": counts["TABLE_PART"],
        "formulas": counts["FORMULA"],
        "figures": counts["FIGURE_CONTEXT"],
        "eligible_elements": len(eligible),
        "mapped_elements": len(eligible & mapped),
        "excluded_elements": len(dataset.excluded_element_ids),
        "missing_provenance": len(missing),
        "omitted_elements": len(omitted),
        "omitted_characters": sum(omitted.values()),
        "split_omitted_elements": len(split_omitted),
        "split_omitted_characters": sum(split_omitted.values()),
        "question_option_splits": option_splits,
        "table_header_loss": sum(
            1 for c in chunks.values() if c.kind == "TABLE_PART" and not c.metadata.get("headers")
        ),
        "tiny": tiny,
        "oversized": oversized,
        "cross_page": cross_page,
        "tiny_ratio": tiny / len(children) if children else None,
        "oversized_ratio": oversized / len(chunks) if chunks else None,
        "cross_page_rate": cross_page / len(chunks) if chunks else None,
        "parent_child_ratio": counts["TEXT_PARENT"] / len(children) if children else None,
        "child_tokens": [c.token_count for c in children],
        "parent_tokens": [c.token_count for c in chunks.values() if c.kind == "TEXT_PARENT"],
        "dataset_hash": digest([c.key for c in dataset.chunks]),
    }
    severities = {f.severity for f in findings}
    result = (
        "FAIL"
        if "CRITICAL" in severities
        else "NEEDS_REVIEW"
        if "ERROR" in severities
        else "PASS_WITH_WARNINGS"
        if "WARNING" in severities
        else "PASS"
    )
    return result, findings, metrics
