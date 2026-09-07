"""Canonical evidence hydration. Every lookup verifies the original tenant/run/version."""

from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.model import ArtifactRef, EvidenceSource, SourceSpan
from app.models.chunking import (
    Chunk,
    ChunkArtifactRelation,
    ChunkSourceElement,
    QuestionArtifact,
    QuestionOption,
)
from app.models.parsing import DocumentElement, FigureArtifact, FormulaArtifact, TableArtifact
from app.repositories.retrieval import hydrate
from app.reranking.model import RerankingError
from app.retrieval.model import RetrievalCorpus


class EvidenceRepository:
    def __init__(self, session: Session, tenant_id: UUID, corpus: RetrievalCorpus) -> None:
        self.session, self.tenant_id, self.corpus = session, tenant_id, corpus

    def source(self, chunk_id: UUID) -> EvidenceSource:
        resolved = hydrate(self.session, self.tenant_id, (chunk_id,), self.corpus, 0)
        if chunk_id not in resolved:
            raise RerankingError("CONTEXT_SOURCE_LINEAGE_MISMATCH")
        chunk = self.session.scalar(
            select(Chunk).where(
                Chunk.id == chunk_id,
                Chunk.tenant_id == self.tenant_id,
                Chunk.chunk_run_id.in_(self.corpus.chunk_run_ids),
            )
        )
        assert chunk is not None
        provenance = resolved[chunk_id][0]
        spans: list[SourceSpan] = []
        hierarchy: list[SourceSpan] = []
        links = self.session.scalars(
            select(ChunkSourceElement)
            .where(
                ChunkSourceElement.chunk_id == chunk.id,
                ChunkSourceElement.chunk_run_id == chunk.chunk_run_id,
                ChunkSourceElement.parse_run_id == chunk.parse_run_id,
            )
            .order_by(ChunkSourceElement.position)
        ).all()
        for link in links:
            element = self.session.scalar(
                select(DocumentElement).where(
                    DocumentElement.id == link.element_id,
                    DocumentElement.tenant_id == self.tenant_id,
                    DocumentElement.parse_run_id == chunk.parse_run_id,
                    DocumentElement.document_version_id == provenance.document_version_id,
                )
            )
            if element is None:
                raise RerankingError("EVIDENCE_PROVENANCE_MISSING")
            text = element.normalized_text or ""
            if not 0 <= link.start_offset <= link.end_offset <= len(text):
                raise RerankingError("EVIDENCE_PROVENANCE_MISSING")
            span = SourceSpan(
                element_id=element.id,
                start=link.start_offset,
                end=link.end_offset,
                text=text[link.start_offset : link.end_offset],
                page=element.page_number,
                reading_order=element.reading_order,
                role=link.role,
                bbox=(element.bbox_x1, element.bbox_y1, element.bbox_x2, element.bbox_y2),
            )
            (hierarchy if link.role == "HIERARCHY" else spans).append(span)
        if not spans:
            raise RerankingError("EVIDENCE_PROVENANCE_MISSING")
        artifacts = []
        relations = self.session.scalars(
            select(ChunkArtifactRelation).where(
                ChunkArtifactRelation.chunk_id == chunk.id,
                ChunkArtifactRelation.chunk_run_id == chunk.chunk_run_id,
                ChunkArtifactRelation.parse_run_id == chunk.parse_run_id,
            )
        ).all()
        base = (
            f"/api/v1/documents/{provenance.document_id}/versions/"
            f"{provenance.document_version_id}/parse-runs/{chunk.parse_run_id}"
        )
        for rel in relations:
            for kind, model, identifier in (
                ("TABLE", TableArtifact, rel.table_id),
                ("FORMULA", FormulaArtifact, rel.formula_id),
                ("FIGURE", FigureArtifact, rel.figure_id),
            ):
                if identifier is None:
                    continue
                artifact = self.session.scalar(
                    select(model).where(
                        model.id == identifier,
                        model.tenant_id == self.tenant_id,
                        model.parse_run_id == chunk.parse_run_id,
                        model.document_version_id == provenance.document_version_id,
                    )
                )
                if not isinstance(artifact, (TableArtifact, FigureArtifact, FormulaArtifact)):
                    raise RerankingError("CONTEXT_ARTIFACT_MISSING")
                ref = ArtifactRef(
                    artifact_id=identifier,
                    kind=kind,
                    source_element_id=artifact.document_element_id,
                    href=f"{base}/{kind.lower()}s/{identifier}"
                    + ("/image" if kind == "FIGURE" else ""),
                )
                if isinstance(artifact, TableArtifact):
                    rows = chunk.chunk_metadata.get("row_indexes", [])
                    headers = chunk.chunk_metadata.get("header_rows", [])
                    selected = [c for c in artifact.cells if c["row"] in set(rows + headers)]
                    if not selected or selected != chunk.chunk_metadata.get("cells"):
                        raise RerankingError("CONTEXT_ARTIFACT_MISSING")
                    ref.row_indexes = rows
                    ref.header_rows = headers
                    ref.cells = selected
                if isinstance(artifact, FigureArtifact):
                    ref.image_available = bool(artifact.image_key)
                if (
                    isinstance(artifact, FormulaArtifact)
                    and (artifact.source_expression or artifact.normalized_expression or "")
                    not in chunk.normalized_text
                ):
                    raise RerankingError("CONTEXT_ARTIFACT_MISSING")
                artifacts.append(ref)
        if (
            chunk.chunk_type in {"TABLE", "TABLE_PART", "FORMULA", "FIGURE_CONTEXT"}
            and not artifacts
        ):
            raise RerankingError("CONTEXT_ARTIFACT_MISSING")
        question: dict[str, Any] | None = None
        evidence_text = None
        source_chunk_ids = [chunk.id]
        if chunk.question_id:
            q = self.session.scalar(
                select(QuestionArtifact).where(
                    QuestionArtifact.id == chunk.question_id,
                    QuestionArtifact.tenant_id == self.tenant_id,
                    QuestionArtifact.chunk_run_id == chunk.chunk_run_id,
                    QuestionArtifact.parse_run_id == chunk.parse_run_id,
                )
            )
            if q is None or q.answer_inferred:
                raise RerankingError("CONTEXT_ARTIFACT_MISSING")
            options = self.session.scalars(
                select(QuestionOption)
                .where(QuestionOption.question_id == q.id)
                .order_by(QuestionOption.ordinal)
            ).all()
            question = {
                "question_id": str(q.id),
                "question_text": q.question_text,
                "options": [{"label": o.label, "text": o.text} for o in options],
                "explicit_answer": q.explicit_answer,
                "explanation": q.explanation,
                "authority": q.authority,
                "assessment_material": True,
            }
        if question is not None:
            # The source-extracted question record is atomic, even when M3 split its explanation.
            parts = [str(question["question_text"])]
            parts.extend(str(o["label"]) + ". " + str(o["text"]) for o in question["options"])
            if question["explicit_answer"] is not None:
                parts.append("Source answer: " + str(question["explicit_answer"]))
            if question["explanation"]:
                parts.append(str(question["explanation"]))
            evidence_text = "\n".join(parts)
            related_ids = list(
                self.session.scalars(
                    select(Chunk.id).where(
                        Chunk.tenant_id == self.tenant_id,
                        Chunk.chunk_run_id == chunk.chunk_run_id,
                        Chunk.question_id == chunk.question_id,
                    )
                ).all()
            )
            source_chunk_ids = sorted(related_ids, key=str)
            related_links = self.session.scalars(
                select(ChunkSourceElement)
                .where(
                    ChunkSourceElement.chunk_id.in_(related_ids),
                    ChunkSourceElement.chunk_run_id == chunk.chunk_run_id,
                    ChunkSourceElement.role != "HIERARCHY",
                )
                .order_by(ChunkSourceElement.position)
            ).all()
            existing = {(span.element_id, span.start, span.end) for span in spans}
            for link in related_links:
                if (link.element_id, link.start_offset, link.end_offset) in existing:
                    continue
                element = self.session.scalar(
                    select(DocumentElement).where(
                        DocumentElement.id == link.element_id,
                        DocumentElement.tenant_id == self.tenant_id,
                        DocumentElement.parse_run_id == chunk.parse_run_id,
                        DocumentElement.document_version_id == provenance.document_version_id,
                    )
                )
                if element is None or link.end_offset > len(element.normalized_text or ""):
                    raise RerankingError("EVIDENCE_PROVENANCE_MISSING")
                spans.append(
                    SourceSpan(
                        element_id=element.id,
                        start=link.start_offset,
                        end=link.end_offset,
                        text=(element.normalized_text or "")[link.start_offset : link.end_offset],
                        page=element.page_number,
                        reading_order=element.reading_order,
                        role=link.role,
                        bbox=(element.bbox_x1, element.bbox_y1, element.bbox_x2, element.bbox_y2),
                    )
                )
                existing.add((link.element_id, link.start_offset, link.end_offset))
        return EvidenceSource(
            tenant_id=self.tenant_id,
            chunk_id=chunk.id,
            source_chunk_ids=source_chunk_ids,
            parse_run_id=chunk.parse_run_id,
            provenance=replace(provenance, source_element_ids=tuple(s.element_id for s in spans)),
            retrieval_text=chunk.retrieval_text,
            evidence_text=evidence_text,
            text=chunk.normalized_text,
            spans=spans,
            hierarchy=hierarchy,
            artifacts=artifacts,
            question=question,
        )

    def relatives(self, source: EvidenceSource) -> list[EvidenceSource]:
        p = source.provenance
        if p.parent_chunk_id is None:
            return []
        parent = self.source(p.parent_chunk_id)
        if (
            parent.provenance.chunk_run_id != p.chunk_run_id
            or parent.provenance.document_version_id != p.document_version_id
        ):
            raise RerankingError("CONTEXT_SOURCE_LINEAGE_MISMATCH")
        relatives = [parent]
        for before in (True, False):
            statement = select(Chunk.id).where(
                Chunk.tenant_id == self.tenant_id,
                Chunk.chunk_run_id == p.chunk_run_id,
                Chunk.parent_chunk_id == p.parent_chunk_id,
            )
            statement = (
                statement.where(Chunk.sequence_number < p.sequence_number).order_by(
                    Chunk.sequence_number.desc()
                )
                if before
                else statement.where(Chunk.sequence_number > p.sequence_number).order_by(
                    Chunk.sequence_number
                )
            )
            identifier = self.session.scalar(statement.limit(1))
            if identifier:
                relatives.append(self.source(identifier))
        return relatives
