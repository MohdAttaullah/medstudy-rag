from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.chunking_config import ChunkingConfig
from app.core.errors import DomainError
from app.ingestion.chunking.errors import ChunkError
from app.ingestion.chunking.model import ChunkInput, SourceArtifact, SourceElement, SourcePage
from app.models.chunking import Chunk, ChunkRun
from app.models.documents import Document, DocumentVersion
from app.models.parsing import (
    DocumentElement,
    DocumentPage,
    FigureArtifact,
    FormulaArtifact,
    ParseRun,
    TableArtifact,
)


def get_run(session: Session, tenant_id: UUID, run_id: UUID) -> ChunkRun:
    run = session.scalar(
        select(ChunkRun).where(ChunkRun.id == run_id, ChunkRun.tenant_id == tenant_id)
    )
    if run is None:
        raise DomainError("CHUNK_RUN_NOT_FOUND", "Chunk run not found.", 404)
    return run


def get_chunk(session: Session, tenant_id: UUID, chunk_id: UUID) -> Chunk:
    chunk = session.scalar(select(Chunk).where(Chunk.id == chunk_id, Chunk.tenant_id == tenant_id))
    if chunk is None:
        raise DomainError("CHUNK_NOT_FOUND", "Chunk not found.", 404)
    get_run(session, tenant_id, chunk.chunk_run_id)
    return chunk


def load_source(session: Session, run: ChunkRun, config: ChunkingConfig) -> ChunkInput:
    parsed = session.get(ParseRun, run.parse_run_id)
    version = session.get(DocumentVersion, run.document_version_id)
    document = session.get(Document, run.document_id)
    if (
        parsed is None
        or not parsed.is_active
        or parsed.status != "SUCCEEDED"
        or parsed.validation_result not in {"PASS", "PASS_WITH_WARNINGS"}
        or version is None
        or document is None
        or document.archived_at
    ):
        raise ChunkError("CHUNK_SOURCE_PARSE_NOT_READY")
    count, characters = session.execute(
        select(
            func.count(), func.coalesce(func.sum(func.length(DocumentElement.normalized_text)), 0)
        ).where(DocumentElement.parse_run_id == parsed.id)
    ).one()
    if count > config.max_source_elements or characters > config.max_source_characters:
        raise ChunkError("CHUNK_RESOURCE_LIMIT")
    pages = list(
        session.scalars(
            select(DocumentPage)
            .where(DocumentPage.parse_run_id == parsed.id)
            .order_by(DocumentPage.page_number)
        )
    )
    rows = list(
        session.scalars(
            select(DocumentElement)
            .where(DocumentElement.parse_run_id == parsed.id)
            .order_by(DocumentElement.reading_order)
        )
    )
    if any(
        e.tenant_id != run.tenant_id or e.document_version_id != version.id for e in rows + pages
    ):
        raise ChunkError("CHUNK_NORMALIZATION_FAILED")
    elements = tuple(
        SourceElement(
            id=e.id,
            page_id=e.page_id,
            page_number=e.page_number,
            reading_order=e.reading_order,
            parent_id=e.parent_element_id,
            kind=e.element_type.value,
            text=e.normalized_text or "",
            raw_text=e.raw_text,
            bbox=cast(
                tuple[float, float, float, float], (e.bbox_x1, e.bbox_y1, e.bbox_x2, e.bbox_y2)
            )
            if all(v is not None for v in (e.bbox_x1, e.bbox_y1, e.bbox_x2, e.bbox_y2))
            else None,
        )
        for e in rows
    )
    artifacts = []
    for model, kind in (
        (TableArtifact, "TABLE"),
        (FigureArtifact, "FIGURE"),
        (FormulaArtifact, "FORMULA"),
    ):
        for artifact in session.scalars(select(model).where(model.parse_run_id == parsed.id)):
            a: Any = artifact
            if a.tenant_id != run.tenant_id or a.document_version_id != version.id:
                raise ChunkError("CHUNK_NORMALIZATION_FAILED")
            related = []
            if kind == "TABLE":
                data = {
                    "row_count": a.row_count,
                    "column_count": a.column_count,
                    "header_row_count": a.header_row_count,
                    "cells": a.cells,
                    "table_group_id": str(a.table_group_id) if a.table_group_id else None,
                    "possible_continuation": a.possible_continuation,
                    "continuation_of_id": str(a.continuation_of_id)
                    if a.continuation_of_id
                    else None,
                }
                related = [
                    e.id
                    for e in rows
                    if e.parent_element_id == a.document_element_id and e.element_type == "FOOTNOTE"
                ]
            elif kind == "FORMULA":
                data = {
                    "source_expression": a.source_expression,
                    "normalized_expression": a.normalized_expression,
                }
                related = [i for i in (a.preceding_element_id, a.following_element_id) if i]
            else:
                data = {"image_available": bool(a.image_key)}
            artifacts.append(
                SourceArtifact(
                    id=a.id,
                    element_id=a.document_element_id,
                    kind=kind,
                    page_number=a.page_number,
                    caption_id=getattr(a, "caption_element_id", None),
                    caption=getattr(a, "caption_text", None),
                    data=data,
                    related_ids=tuple(related),
                )
            )
    return ChunkInput(
        document_id=document.id,
        version_id=version.id,
        parse_run_id=parsed.id,
        title=document.title,
        source_type=document.source_type.value,
        authority_level=document.authority_level.value,
        edition=version.edition,
        publication_year=version.publication_year,
        pages=tuple(
            SourcePage(id=p.id, number=p.page_number, width=p.width, height=p.height) for p in pages
        ),
        elements=elements,
        artifacts=tuple(artifacts),
    )
