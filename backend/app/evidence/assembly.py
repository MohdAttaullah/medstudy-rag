"""Deterministic bounded evidence construction over an immutable authorized snapshot."""

import hashlib
from collections.abc import Callable, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.reranking_config import EvidenceBudgetConfig, ExpansionConfig
from app.evidence.model import EvidenceBlock, EvidenceSource, SourceSpan
from app.reranking.model import RerankingError


class EvidenceAssembler:
    def __init__(
        self, expansion: ExpansionConfig, budget: EvidenceBudgetConfig, count: Callable[[str], int]
    ) -> None:
        self.expansion, self.budget, self.count = expansion, budget, count

    def assemble(
        self, anchors: Sequence[EvidenceSource], relatives: dict[UUID, list[EvidenceSource]]
    ) -> tuple[list[EvidenceBlock], list[str], int]:
        blocks: list[EvidenceBlock] = []
        warnings: list[str] = []
        intervals: dict[tuple[UUID, UUID], list[tuple[int, int]]] = {}
        chunks: set[UUID] = set()
        tables: set[tuple[UUID, UUID, tuple[int, ...]]] = set()
        seen_visual: set[tuple[UUID, UUID]] = set()
        duplicates = 0
        total = 0

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

        def add(source: EvidenceSource, anchor: EvidenceSource, reason: str, limit: int) -> bool:
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
                warnings.append("CONTEXT_BUDGET_EXCEEDED:" + str(source.chunk_id))
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
                        warnings.append("CONTEXT_INVALID_NEIGHBOUR:" + str(relative.chunk_id))
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
        return blocks, list(dict.fromkeys(warnings)), duplicates
