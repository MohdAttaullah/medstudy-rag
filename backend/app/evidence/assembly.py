"""Deterministic bounded evidence construction over an immutable authorized snapshot."""

import hashlib
from collections.abc import Callable, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.reranking_config import EvidenceBudgetConfig, ExpansionConfig
from app.evidence.model import EvidenceBlock, EvidenceSource, EvidenceWarning, SourceSpan
from app.reranking.model import RerankingError


class EvidenceAssembler:
    def __init__(
        self, expansion: ExpansionConfig, budget: EvidenceBudgetConfig, count: Callable[[str], int]
    ) -> None:
        self.expansion, self.budget, self.count = expansion, budget, count

    def assemble(
        self, anchors: Sequence[EvidenceSource], relatives: dict[UUID, list[EvidenceSource]]
    ) -> tuple[list[EvidenceBlock], list[EvidenceWarning], int]:
        blocks: list[EvidenceBlock] = []
        warnings: list[EvidenceWarning] = []
        intervals: dict[tuple[UUID, UUID], list[tuple[int, int]]] = {}
        chunks: set[UUID] = set()
        tables: set[tuple[UUID, UUID, tuple[int, ...]]] = set()
        seen_visual: set[tuple[UUID, UUID]] = set()
        duplicates = 0
        total = 0
        needs_parent: set[UUID] = set()

        def remaining(source: EvidenceSource) -> list[SourceSpan]:
            result = []
            for span in sorted(source.spans, key=lambda s: (s.reading_order, s.start)):
                pieces = [(span.start, span.end)]
                for lo, hi in intervals.get(
                    (source.provenance.document_version_id, span.element_id), []
                ):
                    pieces = [
                        part
                        for a, b in pieces
                        for part in ((a, min(b, lo)), (max(a, hi), b))
                        if part[0] < part[1]
                    ]
                for a, b in pieces:
                    result.append(
                        span.model_copy(
                            update={
                                "start": a,
                                "end": b,
                                "text": span.text[a - span.start : b - span.start],
                            }
                        )
                    )
            return result

        def add(
            source: EvidenceSource,
            anchor: EvidenceSource,
            reason: str,
            limit: int,
            required: bool = False,
        ) -> bool:
            nonlocal total, duplicates
            p = source.provenance
            if (
                source.tenant_id != anchor.tenant_id
                or p.chunk_run_id != anchor.provenance.chunk_run_id
                or p.document_version_id != anchor.provenance.document_version_id
            ):
                raise RerankingError("CONTEXT_SOURCE_LINEAGE_MISMATCH")
            if not source.spans or p.page_start is None:
                raise RerankingError("EVIDENCE_PROVENANCE_MISSING")
            tablekeys = {
                (p.document_version_id, a.artifact_id, tuple(a.row_indexes))
                for a in source.artifacts
                if a.kind == "TABLE"
            }
            if source.chunk_id in chunks or (tablekeys and tablekeys <= tables):
                duplicates += 1
                return False
            spans = remaining(source)
            atomic = bool(source.artifacts or source.question)
            visual_only = any(a.kind == "FIGURE" for a in source.artifacts) and all(
                s.start == s.end for s in source.spans
            )
            visual_keys = {
                (p.document_version_id, a.artifact_id)
                for a in source.artifacts
                if a.kind == "FIGURE"
            }
            if visual_only and visual_keys <= seen_visual:
                duplicates += 1
                return False
            if not spans and not tablekeys and not visual_only:
                duplicates += 1
                return False
            is_anchor = reason == "RERANKED_ANCHOR"
            partial = sum(s.end - s.start for s in spans) < sum(
                s.end - s.start for s in source.spans
            )
            # Whatever `remaining` trimmed was trimmed because an already-admitted block covers
            # it. Verified rather than assumed, so that if the deduplication rule ever changes
            # the gate stops treating a genuinely truncated anchor as complete.
            covered = all(
                any(
                    lo <= start and end <= hi
                    for lo, hi in intervals.get((p.document_version_id, element_id), [])
                )
                for element_id, start, end in _trimmed(source, spans)
            )
            if is_anchor and (atomic or not partial):
                # Atomic source records remain whole; labels and cell separators are structural.
                text = source.evidence_text or source.retrieval_text
                spans = source.spans
            else:
                text = "\n".join(s.text for s in spans if s.text)
            n = self.count(text)
            if n == 0:
                return False
            if (
                n > min(limit, self.budget.max_tokens_per_block)
                or total + n > self.budget.max_total_tokens
                or len(blocks) >= self.budget.max_blocks
            ):
                # Which tier this came from decides whether it is an incomplete answer or an
                # unused candidate, and only this frame knows. See ADR-019.
                warnings.append(
                    EvidenceWarning(
                        code="CONTEXT_BUDGET_EXCEEDED",
                        chunk_id=source.chunk_id,
                        tier="ANCHOR" if is_anchor else "EXPANSION",
                        selected_by_reranker=is_anchor,
                        required_dependency=required,
                    )
                )
                return False
            if not is_anchor and atomic:
                return False
            digest = hashlib.sha256(text.encode()).hexdigest()
            block = EvidenceBlock(
                evidence_id=uuid5(
                    NAMESPACE_URL, f"m6:{anchor.chunk_id}:{source.chunk_id}:{reason}:{digest}"
                ),
                anchor_chunk_id=anchor.chunk_id,
                source_chunk_ids=source.source_chunk_ids or [source.chunk_id],
                source_element_ids=list(dict.fromkeys(s.element_id for s in spans)),
                document_id=p.document_id,
                document_version_id=p.document_version_id,
                chunk_run_id=p.chunk_run_id,
                parse_run_id=source.parse_run_id,
                document_title=p.document_title,
                source_type=p.source_type,
                authority_level=p.authority_level,
                chunk_type=p.chunk_type,
                pages=sorted({s.page for s in spans if s.page is not None}),
                hierarchy=source.hierarchy,
                source_spans=spans,
                text=text,
                representation="m3-source-with-structural-labels-v1"
                if is_anchor and (atomic or not partial)
                else "source-spans-v1",
                trimmed_text_present_elsewhere=covered,
                artifacts=source.artifacts if is_anchor else [],
                question=source.question if is_anchor else None,
                expansion_reason=reason,
                context_reasons=[reason]
                + (
                    (
                        ["TABLE_HEADER_CONTEXT"]
                        if any(a.kind == "TABLE" for a in source.artifacts)
                        else []
                    )
                    + (
                        ["TABLE_FOOTNOTE"]
                        if any(a.kind == "TABLE" for a in source.artifacts)
                        and any(s.role == "RELATED_CONTEXT" for s in source.spans)
                        else []
                    )
                    + (
                        ["FORMULA_DEFINITION_CONTEXT"]
                        if any(a.kind == "FORMULA" for a in source.artifacts)
                        and any(s.role == "RELATED_CONTEXT" for s in source.spans)
                        else []
                    )
                    + (
                        ["FIGURE_CAPTION"]
                        if any(a.kind == "FIGURE" for a in source.artifacts)
                        and any(s.role == "CAPTION" for s in source.spans)
                        else []
                    )
                    + (
                        ["QUESTION_EXPLANATION"]
                        if source.question and source.question.get("explanation")
                        else []
                    )
                ),
                token_count=n,
                requires_visual_evidence=any(a.kind == "FIGURE" for a in source.artifacts),
            )
            blocks.append(block)
            total += n
            chunks.add(source.chunk_id)
            tables.update(tablekeys)
            seen_visual.update(visual_keys)
            for span in spans:
                intervals.setdefault((p.document_version_id, span.element_id), []).append(
                    (span.start, span.end)
                )
            return True

        selected = []
        for anchor in anchors:
            if add(anchor, anchor, "RERANKED_ANCHOR", self.budget.max_tokens_per_block):
                selected.append(anchor)
        # Reserve space for all selected anchors before any optional context.
        for anchor in selected:
            # An anchor whose text does not end a sentence is a fragment: the parent is what
            # completes it, so its absence is a deficiency of selected evidence rather than a
            # missing optional extra. Recorded before the loop so a parent that was never offered
            # is caught as well as one that did not fit.
            if (
                self.expansion.parent_enabled
                and anchor.provenance.chunk_type in {"TEXT_CHILD", "TEXT"}
                and anchor.provenance.parent_chunk_id is not None
                and not anchor.text.rstrip().endswith((".", "!", "?"))
            ):
                needs_parent.add(anchor.chunk_id)
            used = 0
            for relative in relatives.get(anchor.chunk_id, []):
                if used >= self.expansion.max_expansions_per_anchor:
                    break
                if (
                    relative.provenance.chunk_run_id != anchor.provenance.chunk_run_id
                    or relative.tenant_id != anchor.tenant_id
                ):
                    raise RerankingError("CONTEXT_SOURCE_LINEAGE_MISMATCH")
                if relative.chunk_id == anchor.provenance.parent_chunk_id:
                    if not self.expansion.parent_enabled or anchor.provenance.chunk_type not in {
                        "TEXT_CHILD",
                        "TEXT",
                    }:
                        continue
                    # Only short paragraph fragments need missing surrounding source context.
                    if anchor.text.rstrip().endswith((".", "!", "?")):
                        continue
                    reason, limit = "PARENT_EXPANSION", self.expansion.max_parent_tokens
                    needs_parent.discard(anchor.chunk_id)
                    if not add(relative, anchor, reason, limit, required=True):
                        needs_parent.add(anchor.chunk_id)
                    else:
                        used += 1
                    continue
                else:
                    if (
                        anchor.provenance.parent_chunk_id is None
                        or relative.provenance.parent_chunk_id != anchor.provenance.parent_chunk_id
                    ):
                        raise RerankingError("CONTEXT_INVALID_NEIGHBOUR")
                    before = relative.provenance.sequence_number < anchor.provenance.sequence_number
                    if not (
                        self.expansion.previous_siblings if before else self.expansion.next_siblings
                    ):
                        continue
                    left, right = (relative, anchor) if before else (anchor, relative)
                    if (
                        not left.spans
                        or not right.spans
                        or max(s.reading_order for s in left.spans) + 1
                        < min(s.reading_order for s in right.spans)
                    ):
                        warnings.append(
                            EvidenceWarning(
                                code="CONTEXT_INVALID_NEIGHBOUR",
                                chunk_id=relative.chunk_id,
                                tier="EXPANSION",
                            )
                        )
                        continue
                    reason, limit = (
                        ("PREVIOUS_SIBLING" if before else "NEXT_SIBLING"),
                        self.expansion.max_neighbour_tokens,
                    )
                if add(relative, anchor, reason, limit):
                    used += 1
        order = {a.chunk_id: i for i, a in enumerate(selected)}
        blocks.sort(
            key=lambda b: (
                order[b.anchor_chunk_id],
                b.expansion_reason != "RERANKED_ANCHOR",
                min((s.reading_order for s in b.source_spans), default=0),
            )
        )
        for chunk_id in sorted(needs_parent, key=str):
            warnings.append(
                EvidenceWarning(
                    code="CONTEXT_REQUIRED_PARENT_MISSING",
                    chunk_id=chunk_id,
                    tier="ANCHOR",
                    selected_by_reranker=True,
                    required_dependency=True,
                )
            )
        unique = list(dict.fromkeys((w.code, w.chunk_id) for w in warnings))
        ordered = [next(w for w in warnings if (w.code, w.chunk_id) == key) for key in unique]
        return blocks, ordered, duplicates


def _trimmed(source: EvidenceSource, retained: list[SourceSpan]) -> list[tuple[UUID, int, int]]:
    """The regions of a source that were dropped when its spans were deduplicated."""
    kept: dict[UUID, list[tuple[int, int]]] = {}
    for span in retained:
        kept.setdefault(span.element_id, []).append((span.start, span.end))
    removed: list[tuple[UUID, int, int]] = []
    for span in source.spans:
        pieces = [(span.start, span.end)]
        for lo, hi in kept.get(span.element_id, []):
            pieces = [
                part
                for a, b in pieces
                for part in ((a, min(b, lo)), (max(a, hi), b))
                if part[0] < part[1]
            ]
        removed.extend((span.element_id, a, b) for a, b in pieces)
    return removed
