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
    #: The least room a table-row fragment must have for its cell text to mean anything.
    MIN_FRAGMENT_TOKENS = 16

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
        retrieval = self.representation(kind, text, hierarchy)
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

    def prefix(self, hierarchy: tuple[UUID, ...]) -> str:
        if not self.config.include_hierarchy_context or not hierarchy:
            return ""
        return "Context: " + " > ".join(self.elements[e].text for e in hierarchy) + "\n\n"

    def room(self, hierarchy: tuple[UUID, ...], target: int) -> int:
        """What a body may cost once the hierarchy prefix it will be emitted with is paid for.

        Every retrieval unit is embedded as `representation`, prefix included, and validated the
        same way, so a split decided on the source text alone holds only until a document has a
        hierarchy. The prefix ends at a whitespace boundary, so its tokens and the body's add.
        """
        return max(target - self.tokens.count(self.prefix(hierarchy)), 1)

    def representation(self, kind: str, text: str, hierarchy: tuple[UUID, ...]) -> str:
        """Exactly the retrieval text `emit` will store for this body.

        The embedding input's body is this string and not the source text, so anything that
        budgets a retrieval unit has to measure this rather than its own approximation of it.
        `table.rendered` exists for the same reason one stage earlier: a split that measures
        something other than what it emits holds only until a document disagrees.
        """
        retrieval = self.prefix(hierarchy) + text
        if kind == "FIGURE_CONTEXT" and not text:
            retrieval = (
                retrieval + "\nSource figure; no source caption or explanatory text."
            ).strip()
        return retrieval

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

    def figure_parts(
        self, spans: tuple[Span, ...], hierarchy: tuple[UUID, ...]
    ) -> list[tuple[Span, ...]]:
        """Group a figure's spans so that every part is embeddable by construction.

        A long legend is split, never trimmed. The caption element is consumed here, so this is the
        only chunk that carries it: a bounded representation would leave the sentences past the
        bound in no retrieval unit at all, findable through nothing. Splitting keeps every sentence
        in some part, and each part keeps the figure, the full caption metadata and exact source
        offsets, so nothing is invented and nothing is quietly dropped.

        The budget is measured on the retrieval representation, because that is what the encoder
        receives. A real 22-page atlas chapter produced a 641-token legend against a 384-token
        budget, and the previous construction emitted it whole: no valid embedding input could
        carry it, and the contradiction only surfaced two stages later.
        """
        budget = self.config.retrieval_budget_tokens
        room = self.room(hierarchy, budget)
        groups: list[tuple[Span, ...]] = []
        current: list[Span] = []
        for original in spans:
            for span in self.split_span(original, room):
                if current and self.measure(tuple(current + [span]), hierarchy) > budget:
                    groups.append(tuple(current))
                    current = []
                current.append(span)
        if current:
            groups.append(tuple(current))
        # A figure always contributes its own element span, so this holds at least one group. The
        # fallback keeps an empty one legal: it emits the figure alone, which is the no-text case
        # CHUNK_FIGURE_NO_TEXT already describes.
        return groups or [()]

    def measure(self, spans: tuple[Span, ...], hierarchy: tuple[UUID, ...]) -> int:
        """What a figure part would cost as an embedding input body."""
        return self.tokens.count(self.representation("FIGURE_CONTEXT", self.text(spans), hierarchy))

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
            anchor = self.whole(a.element_id)
            whole = self.text(tuple(spans))
            parts = self.figure_parts(tuple(spans), hierarchy)
            for index, part in enumerate(parts):
                # Every part stays mapped to the figure element itself, so no part is a piece of
                # prose that has lost the picture it describes. The anchor carries no text, so it
                # changes neither the body nor its measurement.
                mapped = part if anchor in part else (anchor,) + part
                self.emit(
                    "FIGURE_CONTEXT",
                    mapped,
                    artifact_ids=(a.id,),
                    hierarchy=hierarchy,
                    metadata={
                        # The complete legend, on every part: the exact source stays recoverable
                        # from any one of them, alongside the spans that locate it.
                        "caption": a.caption,
                        # Decided for the figure as a whole and never per part, so splitting a long
                        # legend cannot turn a readable figure into a visual-only one.
                        "visual_only": not bool(whole),
                        "image_available": a.data.get("image_available", False),
                        # Absent unless the legend actually needed splitting, so an ordinary
                        # figure's chunk identity is exactly what it was before this change.
                        **(
                            {"part_number": index + 1, "part_count": len(parts)}
                            if len(parts) > 1
                            else {}
                        ),
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
        ordered = sorted(cells, key=lambda c: (c["row"], c["column"]))

        def label(column: int) -> str:
            """The column's header text, to name a value that appears outside its own row."""
            names = [
                str(c.get("text", "")).strip()
                for c in ordered
                if c["row"] in header_rows and c["column"] <= column < c["column"] + span_of(c)
            ]
            return " / ".join(n for n in names if n) or f"Column {column + 1}"

        def span_of(cell: dict[str, Any]) -> int:
            return int(cell.get("col_span", cell.get("column_span", 1)))

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

        def carried(indexes: list[int]) -> list[dict[str, Any]]:
            """Merged cells that describe rows in this part but are printed on an earlier row.

            A cell spanning rows 2–9 is written once, on row 2. When those rows cannot share one
            part, the later rows would otherwise lose the value entirely — a muscle without its
            innervation. Such a cell is carried, once, into each later part it covers, named by
            its column header and marked as a merged cell, so the part says what the source says
            and nothing more: the value is not copied into each row as if it were separate cells.
            """
            present = set(indexes)
            return [
                c
                for c in ordered
                if c["row"] not in present
                and c["row"] not in header_rows
                and any(c["row"] < i < c["row"] + c.get("row_span", 1) for i in present)
            ]

        def carried_text(indexes: list[int]) -> str:
            note = "(merged cell, also applies to these rows)"
            lines = [f"{label(c['column'])} {note}: {c.get('text', '')}" for c in carried(indexes)]
            return "\n".join(lines) + "\n" if lines else ""

        def rendered(indexes: list[int]) -> str:
            """Exactly what a part will contain, so the budget and the emit cannot diverge.

            Previously the split budgeted `prefix + rows` while emitting `prefix + rows + suffix`,
            so the linked footnotes were never counted. A real antifungal susceptibility table
            stopped at 440 budgeted tokens and was emitted at 545 — over the 450 ceiling by
            exactly the 105 tokens of its footnote legend, and over the encoder's 512-token input
            limit, which failed the whole embedding run two stages later.
            """
            return prefix + carried_text(indexes) + "\n".join(row_text(i) for i in indexes) + suffix

        budget = self.config.table_max_tokens

        def cost(text: str) -> int:
            # The retrieval representation, hierarchy prefix included: that is what is embedded.
            return self.tokens.count(self.representation("TABLE_PART", text, hierarchy))

        # Merged rows stay together whenever they fit. A row group that cannot fit even on its
        # own is the case that produced a 396-token part against a 384-token budget: one cell
        # spanned eight rows, so the group was indivisible and was emitted whole. Such a group is
        # split at row boundaries instead, with its merged cells carried (see `carried`).
        units: list[list[int]] = []
        for group in groups:
            if len(group) > 1 and cost(rendered(group)) > budget:
                units.extend([i] for i in group)
            else:
                units.append(group)

        parts: list[list[int]] = []
        current: list[int] = []
        for unit in units:
            if current and cost(rendered(current + unit)) > budget:
                parts.append(current)
                current = []
            current.extend(unit)
        if current or not parts:
            parts.append(current)

        # A single row too large for any part is divided by cells rather than emitted whole.
        pieces: list[tuple[list[int], str, dict[str, Any]]] = []
        for part in parts:
            text = rendered(part)
            if len(part) == 1 and cost(text) > budget:
                pieces.extend(
                    self.table_row_fragments(
                        part[0],
                        text,
                        prefix,
                        carried(part),
                        suffix,
                        ordered,
                        label,
                        cost,
                        budget,
                    )
                )
            else:
                pieces.append((part, text, {}))

        for i, (part, text, extra) in enumerate(pieces):
            selected = [c for c in cells if c["row"] in set(part) | header_rows]
            self.emit(
                "TABLE" if len(pieces) == 1 else "TABLE_PART",
                spans,
                source_text=text,
                artifact_ids=(a.id,),
                hierarchy=hierarchy,
                metadata={
                    "part_number": i + 1,
                    "part_count": len(pieces),
                    "row_indexes": part,
                    "header_rows": sorted(header_rows),
                    "headers": header,
                    "caption": caption,
                    "cells": selected,
                    "table_group_id": a.data.get("table_group_id"),
                    "possible_continuation": a.data.get("possible_continuation", False),
                    "continuation_of_id": a.data.get("continuation_of_id"),
                    # Absent unless a merged cell had to be repeated, so an ordinary table's
                    # chunk identity is unchanged by the mechanism that handles this case.
                    **(
                        {"carried_cells": [dict(c) for c in carried(part)]} if carried(part) else {}
                    ),
                    **extra,
                },
            )

    def table_row_fragments(
        self,
        row: int,
        whole: str,
        head: str,
        merged: list[dict[str, Any]],
        suffix: str,
        ordered: list[dict[str, Any]],
        label: Callable[[int], str],
        cost: Callable[[str], int],
        budget: int,
    ) -> list[tuple[list[int], str, dict[str, Any]]]:
        """Divide one row that no part can hold, keeping every cell and its column name.

        `head` is the table context (caption and header rows). Each fragment repeats it and the
        row's first cell, which names the row, and writes everything else as `Column: value` —
        including merged cells carried from an earlier row, which can be long enough on their own
        to fill a part — so a value never stands without its header. A value too long even for
        that is cut at sentence boundaries where possible and at token boundaries otherwise, each
        piece marked as continued. All the text appears, in order, in exactly one fragment.

        When the context alone leaves too little room to say anything, the row is emitted whole
        and chunk validation refuses it (CHUNK_OVERSIZED): shredding it into near-empty pieces
        would satisfy the budget while making every piece meaningless.
        """
        cells = [c for c in ordered if c["row"] == row]
        anchor = ""
        body = cells
        if len(cells) > 1:
            anchor = f"{label(cells[0]['column'])}: {cells[0].get('text', '')}\n"
            body = cells[1:]
        lead = head + anchor
        if budget - cost(lead + "Column (continued): " + suffix) < self.MIN_FRAGMENT_TOKENS:
            return [([row], whole, {})]
        lines: list[str] = []
        named = [
            (f"{label(c['column'])} (merged cell, also applies to this row)", c) for c in merged
        ]
        for name, cell in named + [(label(c["column"]), c) for c in body]:
            room = budget - cost(lead + f"{name} (continued): " + suffix)
            for index, piece in enumerate(self.slice_text(str(cell.get("text", "")), room)):
                lines.append(f"{name}{' (continued)' if index else ''}: {piece}")
        fragments: list[list[str]] = []
        current: list[str] = []
        for line in lines:
            if current and cost(lead + "\n".join(current + [line]) + suffix) > budget:
                fragments.append(current)
                current = []
            current.append(line)
        if current:
            fragments.append(current)
        return [
            (
                [row],
                lead + "\n".join(fragment) + suffix,
                {"row_fragment": {"number": n + 1, "count": len(fragments)}},
            )
            for n, fragment in enumerate(fragments)
        ]

    def slice_text(self, text: str, budget: int) -> list[str]:
        """Cut `text` into pieces of at most `budget` tokens, in order, losing no word.

        Sentence boundaries first, then tokenizer offsets into the original string — the same
        order of preference as `split_span`, for text that lives in a table cell rather than in
        an element's own offsets.
        """
        if self.tokens.count(text) <= budget:
            return [text]
        pieces: list[str] = []
        boundaries = [m.end() for m in re.finditer(r"(?<=[.!?;,])\s+", text)] + [len(text)]
        start = 0
        current = 0
        for end in boundaries:
            if self.tokens.count(text[current:end]) <= budget:
                start = end
                continue
            if start > current:
                pieces.append(text[current:start])
                current = start
            while self.tokens.count(text[current:end]) > budget:
                self.checkpoint()
                offsets = self.tokens.offsets(text[current:end])
                stop = current + offsets[budget][0]
                if stop <= current:
                    stop = current + max(offsets[budget - 1][1], 1)
                pieces.append(text[current:stop])
                current = stop
            start = end
        if current < len(text):
            pieces.append(text[current:])
        return [p for p in pieces if p.strip()] or [text]

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
            # A region shares one ancestry, so every child in it is emitted with the same prefix.
            room = self.room(self.ancestry(region[0].id), self.config.child_target_tokens)
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
                    # A large list splits only between complete items. An item too large for any
                    # chunk on its own is split at sentence boundaries rather than emitted whole,
                    # and parts after the first repeat the heading, so it is budgeted for here.
                    repeated = (
                        self.tokens.count(self.text((group[0],)))
                        if e.text.rstrip().endswith(":")
                        else 0
                    )
                    pieces = self.pack(tuple(group), max(room - repeated, 1))
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
                    pair = (self.whole(e.id), self.whole(region[i + 1].id))
                    # Kept as one unit when it fits; otherwise the label leads the first part.
                    units.extend(("TEXT_CHILD", part) for part in self.pack(pair, room))
                    i += 2
                    continue
                packed = self.pack((self.whole(e.id),), room)
                units.extend(("TEXT_CHILD", s) for s in packed)
                i += 1
            children: list[tuple[str, tuple[Span, ...]]] = []
            for kind, spans in units:
                if (
                    children
                    and kind == children[-1][0] == "TEXT_CHILD"
                    and self.tokens.count(self.text(children[-1][1] + spans)) <= room
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
                room = self.room(hierarchy, self.config.explanation_max_tokens)
                # The explanation joins the question only if the joined chunk still fits. Fitting
                # on its own was not enough: question plus explanation could then exceed the budget
                # together, an atomic chunk that no valid embedding input can carry.
                if explanation and self.tokens.count(self.text(body + explanation)) <= room:
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
                for part in self.pack(explanation, room):
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
                    hierarchy = self.ancestry(e.id)
                    preamble = (Span(element_id=e.id, end=end),)
                    room = self.room(hierarchy, self.config.child_target_tokens)
                    for part in self.pack(preamble, room):
                        self.emit("OTHER_STRUCTURED", part, hierarchy=hierarchy)
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
