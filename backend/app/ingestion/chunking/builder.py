"""Structure-aware deterministic construction over a frozen normalized source dataset."""

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.core.chunking_config import ChunkingConfig
from app.ingestion.chunking.errors import ChunkError
from app.ingestion.chunking.model import (
    ChunkDataset,
    ChunkInput,
    DraftChunk,
    Finding,
    QuestionDraft,
    SourceArtifact,
    SourceElement,
    Span,
)
from app.ingestion.chunking.questions import extract
from app.ingestion.chunking.tokenizer import TokenCounter


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def input_fingerprint(source: ChunkInput) -> str:
    return digest(source.model_dump(mode="json"))


class Builder:
    def __init__(
        self,
        source: ChunkInput,
        config: ChunkingConfig,
        tokenizer: TokenCounter,
        checkpoint: Callable[[], None] = lambda: None,
        phase: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.source, self.config, self.tokens, self.checkpoint = (
            source,
            config,
            tokenizer,
            checkpoint,
        )
        # Phases are announced from the point the corresponding work actually begins, so a
        # stalled run is attributable. A phase that has no source material is never announced.
        self.announce, self.announced = phase, set[str]()
        self.elements = {e.id: e for e in source.elements}
        self.pages = {p.id: p for p in source.pages}
        self.artifacts = {a.id: a for a in source.artifacts}
        self.output: list[DraftChunk] = []
        self.findings: list[Finding] = []
        self.consumed: set[UUID] = set()
        self.questions: list[QuestionDraft] = []
        if len(self.elements) != len(source.elements):
            raise ChunkError("CHUNK_NORMALIZATION_FAILED")

    def stage(self, name: str) -> None:
        if name not in self.announced:
            self.announced.add(name)
            self.announce(name)

    def text(self, spans: tuple[Span, ...]) -> str:
        return "\n".join(self.elements[s.element_id].text[s.start : s.end] for s in spans).strip()

    def whole(self, element_id: UUID, role: str = "SOURCE") -> Span:
        e = self.elements[element_id]
        return Span(element_id=e.id, end=len(e.text), role=role)

    def ancestry(self, element_id: UUID) -> tuple[UUID, ...]:
        result: list[UUID] = []
        current = self.elements[element_id].parent_id
        visited = {element_id}
        while current:
            if current in visited or current not in self.elements:
                raise ChunkError("CHUNK_NORMALIZATION_FAILED")
            visited.add(current)
            element = self.elements[current]
            # Only declared parent links provide hierarchy. A changed paragraph/caption label
            # cannot invent a heading or a boundary.
            if element.text:
                result.append(current)
            current = element.parent_id
        return tuple(reversed(result))

    def emit(
        self,
        kind: str,
        spans: tuple[Span, ...],
        *,
        source_text: str | None = None,
        artifact_ids: tuple[UUID, ...] = (),
        hierarchy: tuple[UUID, ...] = (),
        parent: str | None = None,
        question: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DraftChunk:
        self.checkpoint()
        text = self.text(spans) if source_text is None else source_text
        context = []
        if self.config.include_hierarchy_context:
            context = [self.elements[e].text for e in hierarchy]
        retrieval = ("Context: " + " > ".join(context) + "\n\n" if context else "") + text
        if kind == "FIGURE_CONTEXT" and not text:
            retrieval = (
                retrieval + "\nSource figure; no source caption or explanatory text."
            ).strip()
        pages = sorted(
            {n for s in spans if (n := self.elements[s.element_id].page_number) is not None}
        )
        key = digest(
            {
                "version": self.source.version_id,
                "parse": self.source.parse_run_id,
                "policy": self.config.fingerprint,
                "kind": kind,
                "text": text,
                "retrieval": retrieval,
                "spans": [s.model_dump(mode="json") for s in spans],
                "artifacts": artifact_ids,
                "hierarchy": hierarchy,
                "metadata": metadata or {},
            }
        )
        chunk = DraftChunk(
            key=key,
            kind=kind,
            sequence=len(self.output) + 1,
            source_text=text,
            retrieval_text=retrieval,
            token_count=self.tokens.count(text),
            retrieval_token_count=self.tokens.count(retrieval),
            spans=spans,
            artifact_ids=artifact_ids,
            hierarchy=hierarchy,
            parent_key=parent,
            question_key=question,
            page_start=pages[0] if pages else None,
            page_end=pages[-1] if pages else None,
            metadata=metadata or {},
        )
        self.output.append(chunk)
        return chunk

    def split_span(self, span: Span, budget: int) -> list[Span]:
        text = self.elements[span.element_id].text[span.start : span.end]
        if self.tokens.count(text) <= budget:
            return [span]
        # Prefer sentence boundaries. Offsets refer to normalized source, never tokenizer decoding.
        boundaries = [m.end() for m in re.finditer(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)]
        boundaries.append(len(text))
        result = []
        start = 0
        for end in boundaries:
            segment = text[start:end]
            offsets = self.tokens.offsets(segment)
            if len(offsets) <= budget:
                result.append(
                    Span(
                        element_id=span.element_id,
                        start=span.start + start,
                        end=span.start + end,
                        role=span.role,
                    )
                )
            else:
                cursor = 0
                while cursor < len(segment):
                    self.checkpoint()
                    remaining = segment[cursor:]
                    encoded = self.tokens.offsets(remaining)
                    if len(encoded) <= budget:
                        stop = len(segment)
                    else:
                        stop = cursor + encoded[budget][0]
                        if stop <= cursor:
                            stop = cursor + max(encoded[budget - 1][1], 1)
                    result.append(
                        Span(
                            element_id=span.element_id,
                            start=span.start + start + cursor,
                            end=span.start + start + stop,
                            role=span.role,
                        )
                    )
                    cursor = stop
            start = end
        return result

    def pack(
        self, spans: tuple[Span, ...], budget: int, split: bool = True
    ) -> list[tuple[Span, ...]]:
        groups: list[tuple[Span, ...]] = []
        current: list[Span] = []
        for original in spans:
            for span in self.split_span(original, budget) if split else [original]:
                if current and self.tokens.count(self.text(tuple(current + [span]))) > budget:
                    groups.append(tuple(current))
                    current = []
                current.append(span)
        if current:
            groups.append(tuple(current))
        return groups

    def margins(self) -> set[UUID]:
        if self.config.include_repeated_margins:
            return set()
        candidates: dict[str, list[SourceElement]] = defaultdict(list)
        for e in self.source.elements:
            if (
                not e.text
                or len(e.text) > self.config.margin_max_characters
                or not e.bbox
                or e.page_id not in self.pages
            ):
                continue
            page = self.pages[e.page_id]
            if e.bbox[3] <= page.height * self.config.margin_fraction or e.bbox[
                1
            ] >= page.height * (1 - self.config.margin_fraction):
                key = re.sub(r"\d+", "#", e.text.casefold()).strip()
                candidates[key].append(e)
        return {
            e.id
            for values in candidates.values()
            if len({e.page_number for e in values}) >= self.config.repeated_margin_min_pages
            for e in values
        }

    def related(self, a: SourceArtifact, limit: int) -> tuple[Span, ...]:
        candidates = [self.elements[i] for i in a.related_ids if i in self.elements]
        return tuple(
            self.whole(e.id, "RELATED_CONTEXT")
            for e in candidates[:limit]
            if e.text and e.page_number == a.page_number
        )

    def artifact(self, a: SourceArtifact) -> None:
        if a.element_id not in self.elements:
            raise ChunkError("CHUNK_NORMALIZATION_FAILED")
        spans = [self.whole(a.element_id)]
        if a.caption_id:
            if a.caption_id not in self.elements:
                raise ChunkError("CHUNK_NORMALIZATION_FAILED")
            spans.append(self.whole(a.caption_id, "CAPTION"))
            self.consumed.add(a.caption_id)
        self.consumed.add(a.element_id)
        hierarchy = self.ancestry(a.element_id)
        if a.kind == "TABLE":
            self.stage("CHUNK_TABLES_STARTED")
            spans.extend(self.related(a, len(a.related_ids)))
            self.table(a, tuple(spans), hierarchy)
        elif a.kind == "FORMULA":
            self.stage("CHUNK_FORMULAS_STARTED")
            expression = (
                a.data.get("source_expression") or a.data.get("normalized_expression") or ""
            )
            spans.extend(self.related(a, self.config.formula_neighbour_elements))
            context = self.text(tuple(s for s in spans if s.element_id != a.element_id))
            self.emit(
                "FORMULA",
                tuple(spans),
                source_text=expression + ("\n" + context if context else ""),
                artifact_ids=(a.id,),
                hierarchy=hierarchy,
                metadata={
                    "expression": expression,
                    "context_rule": "M2 explicit adjacent-element links",
                },
            )
        elif a.kind == "FIGURE":
            self.stage("CHUNK_FIGURES_STARTED")
            spans.extend(self.related(a, self.config.figure_neighbour_elements))
            self.emit(
                "FIGURE_CONTEXT",
                tuple(spans),
                artifact_ids=(a.id,),
                hierarchy=hierarchy,
                metadata={
                    "caption": a.caption,
                    "visual_only": not bool(self.text(tuple(spans))),
                    "image_available": a.data.get("image_available", False),
                },
            )

    def table(
        self, a: SourceArtifact, spans: tuple[Span, ...], hierarchy: tuple[UUID, ...]
    ) -> None:
        rows, columns = a.data.get("row_count", 0), a.data.get("column_count", 0)
        cells = a.data.get("cells", [])
        if (
            not isinstance(rows, int)
            or not isinstance(columns, int)
            or rows <= 0
            or columns <= 0
            or not cells
        ):
            raise ChunkError("CHUNK_TABLE_FAILED")
        for cell in cells:
            r, c = cell.get("row", -1), cell.get("column", -1)
            rs, cs = cell.get("row_span", 1), cell.get("col_span", cell.get("column_span", 1))
            if (
                not all(isinstance(x, int) for x in (r, c, rs, cs))
                or min(r, c) < 0
                or min(rs, cs) < 1
                or r + rs > rows
                or c + cs > columns
            ):
                raise ChunkError("CHUNK_TABLE_FAILED")
        header_rows = set(range(a.data.get("header_row_count", 0)))
        for c in cells:
            if c.get("column_header", False) or c.get("is_column_header", False):
                header_rows.update(range(c["row"], c["row"] + c.get("row_span", 1)))

        def row_text(index: int) -> str:
            # Preserve canonical cell text and explicit spans; do not replicate merged values
            # into other columns as if the source contained independent cells.
            return " | ".join(
                str(c.get("text", ""))
                for c in sorted(cells, key=lambda c: (c["row"], c["column"]))
                if c["row"] == index
            )

        header = "\n".join(row_text(r) for r in sorted(header_rows))
        caption = a.caption or ""
        prefix = (caption + "\n" if caption else "") + (header + "\n" if header else "")
        footnotes = self.text(tuple(s for s in spans if s.role == "RELATED_CONTEXT"))
        suffix = "\n" + footnotes if footnotes else ""
        groups: list[list[int]] = []
        r = 0
        while r < rows:
            if r in header_rows:
                r += 1
                continue
            end = r + 1
            while True:
                extended = max(
                    [c["row"] + c.get("row_span", 1) for c in cells if r <= c["row"] < end] + [end]
                )
                if extended == end:
                    break
                end = extended
            groups.append([i for i in range(r, end) if i not in header_rows])
            r = end

        def rendered(indexes: list[int]) -> str:
            """Exactly what a part will contain, so the budget and the emit cannot diverge.

            Previously the split budgeted `prefix + rows` while emitting `prefix + rows + suffix`,
            so the linked footnotes were never counted. A real antifungal susceptibility table
            stopped at 440 budgeted tokens and was emitted at 545 — over the 450 ceiling by
            exactly the 105 tokens of its footnote legend, and over the encoder's 512-token input
            limit, which failed the whole embedding run two stages later.
            """
            return prefix + "\n".join(row_text(i) for i in indexes) + suffix

        parts: list[list[int]] = []
        current: list[int] = []
        for group in groups:
            if (
                current
                and self.tokens.count(rendered(current + group)) > self.config.table_max_tokens
            ):
                parts.append(current)
                current = []
            current.extend(group)
        if current or not parts:
            parts.append(current)
        for i, part in enumerate(parts):
            selected = [c for c in cells if c["row"] in set(part) | header_rows]
            self.emit(
                "TABLE" if len(parts) == 1 else "TABLE_PART",
                spans,
                source_text=rendered(part),
                artifact_ids=(a.id,),
                hierarchy=hierarchy,
                metadata={
                    "part_number": i + 1,
                    "part_count": len(parts),
                    "row_indexes": part,
                    "header_rows": sorted(header_rows),
                    "headers": header,
                    "caption": caption,
                    "cells": selected,
                    "table_group_id": a.data.get("table_group_id"),
                    "possible_continuation": a.data.get("possible_continuation", False),
                    "continuation_of_id": a.data.get("continuation_of_id"),
                },
            )

    def generic(self, elements: list[SourceElement]) -> None:
        if elements:
            self.stage("CHUNK_TEXT_STARTED")
        # Declared ancestry forms structural regions. Labels alone never group captions or
        # paragraphs differently. A numbered heading also starts a deterministic region.
        regions: list[list[SourceElement]] = []
        region: list[SourceElement] = []
        last: tuple[UUID, ...] | None = None
        for e in elements:
            hierarchy = self.ancestry(e.id)
            heading = bool(re.match(r"^\d+(?:\.\d+)+\s+\S", e.text))
            if region and (hierarchy != last or heading):
                regions.append(region)
                region = []
            region.append(e)
            last = hierarchy
        if region:
            regions.append(region)
        for region in regions:
            units: list[tuple[str, tuple[Span, ...]]] = []
            i = 0
            while i < len(region):
                e = region[i]

                def bullet(value: str) -> bool:
                    return bool(re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", value))

                if bullet(e.text) or (
                    e.text.rstrip().endswith(":")
                    and i + 1 < len(region)
                    and bullet(region[i + 1].text)
                ):
                    group = [self.whole(e.id)]
                    i += 1
                    while i < len(region) and bullet(region[i].text):
                        group.append(self.whole(region[i].id))
                        i += 1
                    # List items stay atomic; a large list splits only between complete items.
                    pieces = self.pack(tuple(group), self.config.child_target_tokens, split=False)
                    for part_index, part in enumerate(pieces):
                        if (
                            part_index
                            and group[0].element_id != part[0].element_id
                            and e.text.rstrip().endswith(":")
                        ):
                            part = (group[0].model_copy(update={"role": "LIST_HEADING"}),) + part
                        units.append(("LIST", part))
                    continue
                # Preserve an explicit term label plus following definition.
                if e.text.rstrip().endswith(":") and i + 1 < len(region):
                    units.append(("TEXT_CHILD", (self.whole(e.id), self.whole(region[i + 1].id))))
                    i += 2
                    continue
                packed = self.pack((self.whole(e.id),), self.config.child_target_tokens)
                units.extend(("TEXT_CHILD", s) for s in packed)
                i += 1
            children: list[tuple[str, tuple[Span, ...]]] = []
            for kind, spans in units:
                if (
                    children
                    and kind == children[-1][0] == "TEXT_CHILD"
                    and self.tokens.count(self.text(children[-1][1] + spans))
                    <= self.config.child_target_tokens
                ):
                    children[-1] = (kind, children[-1][1] + spans)
                else:
                    children.append((kind, spans))
            parent_parent_groups: list[list[tuple[str, tuple[Span, ...]]]] = []
            parent_group: list[tuple[str, tuple[Span, ...]]] = []
            for child in children:
                joined = tuple(s for _, ss in parent_group + [child] for s in ss)
                if (
                    parent_group
                    and self.tokens.count(self.text(joined)) > self.config.parent_target_tokens
                ):
                    parent_parent_groups.append(parent_group)
                    parent_group = []
                parent_group.append(child)
            if parent_group:
                parent_parent_groups.append(parent_group)
            for parent_group in parent_parent_groups:
                spans = tuple(s for _, ss in parent_group for s in ss)
                hierarchy = self.ancestry(spans[0].element_id)
                parent = self.emit("TEXT_PARENT", spans, hierarchy=hierarchy)
                for kind, part in parent_group:
                    self.emit(kind, part, hierarchy=hierarchy, parent=parent.key)

    def build(self) -> ChunkDataset:
        if (
            len(self.source.elements) > self.config.max_source_elements
            or sum(len(e.text) for e in self.source.elements) > self.config.max_source_characters
        ):
            raise ChunkError("CHUNK_RESOURCE_LIMIT")
        excluded = self.margins()
        for artifact in sorted(
            self.source.artifacts,
            key=lambda a: (
                self.elements[a.element_id].reading_order if a.element_id in self.elements else -1,
                str(a.id),
            ),
        ):
            self.artifact(artifact)
        candidates = [
            e
            for e in sorted(self.source.elements, key=lambda e: (e.reading_order, str(e.id)))
            if e.id not in excluded | self.consumed and e.text.strip()
        ]
        if self.source.source_type in {"QUESTION_BANK", "QUESTION_PAPER", "ANSWER_KEY"}:
            authority: dict[str, object] = {
                "source_type": self.source.source_type,
                "authority_level": self.source.authority_level,
                "edition": self.source.edition,
                "publication_year": self.source.publication_year,
            }
            self.stage("CHUNK_QUESTIONS_STARTED")
            questions = extract(candidates, authority)
            used: dict[UUID, list[Span]] = defaultdict(list)
            for question, body, explanation in questions:
                self.questions.append(question)
                for s in question.source_spans:
                    used[s.element_id].append(s)
                hierarchy = self.ancestry(body[0].element_id)
                if self.tokens.count(self.text(explanation)) <= self.config.explanation_max_tokens:
                    body = body + explanation
                    explanation = ()
                parent = self.emit(
                    "QUESTION",
                    body,
                    hierarchy=hierarchy,
                    question=question.key,
                    metadata={
                        "extraction_status": question.extraction_status,
                        "authority": authority,
                    },
                )
                for part in self.pack(explanation, self.config.explanation_max_tokens):
                    self.emit(
                        "QUESTION_EXPLANATION",
                        part,
                        hierarchy=hierarchy,
                        parent=parent.key,
                        question=question.key,
                        metadata={"question_context_in_parent": True},
                    )
                if question.extraction_status == "NEEDS_REVIEW":
                    self.findings.append(
                        Finding(
                            severity="ERROR",
                            code="CHUNK_QUESTION_BOUNDARIES",
                            message="Question option ordering or stem requires review.",
                            chunk_key=parent.key,
                        )
                    )
            # Preserve preamble source instead of silently discarding it.
            remaining = []
            for e in candidates:
                if e.id not in used:
                    remaining.append(e)
                elif min(s.start for s in used[e.id]) > 0:
                    end = min(s.start for s in used[e.id])
                    # Generic uses full source offsets; emit any partial preamble directly.
                    self.emit(
                        "OTHER_STRUCTURED",
                        (Span(element_id=e.id, end=end),),
                        hierarchy=self.ancestry(e.id),
                    )
            candidates = remaining
        self.generic(candidates)
        # Order complete parent/child groups by source reading order, keeping parents first.
        roots = {c.key: c for c in self.output if c.parent_key is None}
        root_order = {
            key: min(self.elements[s.element_id].reading_order for s in c.spans)
            for key, c in roots.items()
        }
        ordered = sorted(
            self.output,
            key=lambda c: (
                root_order[c.parent_key or c.key],
                roots[c.parent_key or c.key].sequence,
                c.sequence,
            ),
        )
        return ChunkDataset(
            chunks=tuple(c.model_copy(update={"sequence": i + 1}) for i, c in enumerate(ordered)),
            questions=tuple(self.questions),
            findings=tuple(self.findings),
            excluded_element_ids=tuple(sorted(excluded, key=str)),
            input_fingerprint=input_fingerprint(self.source),
        )
