"""Loading retrieval-eligible chunks and their provenance, and tenant-scoped M4 lookups."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.embedding_config import EmbeddingConfig
from app.core.errors import DomainError
from app.embeddings.errors import EmbeddingError
from app.embeddings.inputs import ChunkSource, eligible
from app.models.chunking import Chunk, ChunkRun, ChunkSourceElement
from app.models.documents import Document, DocumentVersion
from app.models.embeddings import EmbeddingRun, EmbeddingVersion, IndexRun
from app.models.parsing import DocumentElement


def get_embedding_run(session: Session, tenant_id: UUID, run_id: UUID) -> EmbeddingRun:
    run = session.scalar(
        select(EmbeddingRun).where(EmbeddingRun.id == run_id, EmbeddingRun.tenant_id == tenant_id)
    )
    if run is None:
        raise DomainError("EMBEDDING_RUN_NOT_FOUND", "Embedding run not found.", 404)
    return run


def get_index_run(session: Session, tenant_id: UUID, run_id: UUID) -> IndexRun:
    run = session.scalar(
        select(IndexRun).where(IndexRun.id == run_id, IndexRun.tenant_id == tenant_id)
    )
    if run is None:
        raise DomainError("INDEX_RUN_NOT_FOUND", "Index run not found.", 404)
    return run


def get_embedding_version(session: Session, version_id: UUID) -> EmbeddingVersion:
    version = session.get(EmbeddingVersion, version_id)
    if version is None:
        raise DomainError("EMBEDDING_VERSION_NOT_FOUND", "Embedding version not found.", 404)
    return version


def hierarchy_labels(session: Session, chunk: Chunk) -> tuple[str, ...]:
    """Declared hierarchy for a chunk, in source order.

    M3 persists ancestry as source-element mappings with role HIERARCHY, so the context field is
    assembled from real parsed headings rather than from anything inferred at embedding time.
    """
    rows = session.execute(
        select(DocumentElement.normalized_text)
        .join(
            ChunkSourceElement,
            (ChunkSourceElement.element_id == DocumentElement.id)
            & (ChunkSourceElement.parse_run_id == DocumentElement.parse_run_id),
        )
        .where(
            ChunkSourceElement.chunk_id == chunk.id,
            ChunkSourceElement.role == "HIERARCHY",
        )
        .order_by(ChunkSourceElement.position)
    ).all()
    return tuple(text for (text,) in rows if text)


def _caption(chunk: Chunk) -> str | None:
    value = chunk.chunk_metadata.get("caption") if chunk.chunk_metadata else None
    return str(value) if isinstance(value, str) and value.strip() else None


def load_sources(
    session: Session, run: EmbeddingRun, config: EmbeddingConfig
) -> tuple[tuple[ChunkSource, ...], dict[UUID, Chunk], Document, DocumentVersion]:
    """Retrieval-eligible chunks of the run's chunk dataset, with the facts an input may use."""
    dataset = session.get(ChunkRun, run.chunk_run_id)
    version = session.get(DocumentVersion, run.document_version_id)
    document = session.get(Document, run.document_id)
    if (
        dataset is None
        or not dataset.is_active
        or dataset.status != "SUCCEEDED"
        or dataset.validation_result not in {"PASS", "PASS_WITH_WARNINGS"}
        or version is None
        or document is None
        or document.archived_at
    ):
        raise EmbeddingError("EMBEDDING_SOURCE_NOT_READY")

    rows = list(
        session.scalars(
            select(Chunk)
            .where(Chunk.chunk_run_id == dataset.id, Chunk.tenant_id == run.tenant_id)
            .order_by(Chunk.sequence_number)
        )
    )
    selected = [chunk for chunk in rows if eligible(chunk.chunk_type, config)]
    if len(selected) > config.max_chunks:
        raise EmbeddingError("EMBEDDING_RESOURCE_LIMIT", {"chunks": len(selected)})

    sources = tuple(
        ChunkSource(
            chunk_id=chunk.id,
            chunk_type=chunk.chunk_type,
            retrieval_text=chunk.retrieval_text,
            document_title=document.title,
            hierarchy=hierarchy_labels(session, chunk),
            caption=_caption(chunk),
        )
        for chunk in selected
    )
    return sources, {chunk.id: chunk for chunk in selected}, document, version


def payload_for(
    chunk: Chunk,
    document: Document,
    run: EmbeddingRun,
    index_run_id: UUID | None = None,
) -> dict[str, Any]:
    """Routing, filtering and provenance only.

    Chunk text is deliberately absent. PostgreSQL is the canonical store for source content, M5
    resolves chunk ids there anyway, and copying licensed medical bodies into a second system
    would widen the blast radius of any index exposure for no retrieval benefit.
    """
    payload: dict[str, Any] = {
        "tenant_id": str(run.tenant_id),
        "document_id": str(document.id),
        "document_version_id": str(run.document_version_id),
        "chunk_run_id": str(run.chunk_run_id),
        "embedding_run_id": str(run.id),
        "embedding_version_id": str(run.embedding_version_id),
        "chunk_id": str(chunk.id),
        "parent_chunk_id": str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None,
        "question_id": str(chunk.question_id) if chunk.question_id else None,
        "chunk_type": chunk.chunk_type,
        # Assessment material stays distinguishable from reference evidence, so a later retrieval
        # stage can filter or weight it. Being indexed never promotes a question key to authority.
        "source_type": document.source_type.value,
        "authority_level": document.authority_level.value,
        "subject": document.subject,
        "specialty": document.specialty,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "sequence_number": chunk.sequence_number,
        "chunk_hash": chunk.chunk_hash,
    }
    if index_run_id is not None:
        payload["index_run_id"] = str(index_run_id)
    return payload
