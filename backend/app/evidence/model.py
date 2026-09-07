"""Request-scoped, provenance-bearing evidence contracts. Never an answer."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.retrieval.model import Provenance


class SourceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)
    element_id: UUID
    start: int
    end: int
    text: str
    page: int | None
    reading_order: int
    role: str
    bbox: tuple[float | None, float | None, float | None, float | None]


class ArtifactRef(BaseModel):
    artifact_id: UUID
    kind: str
    source_element_id: UUID
    href: str
    row_indexes: list[int] = Field(default_factory=list)
    header_rows: list[int] = Field(default_factory=list)
    cells: list[dict[str, Any]] = Field(default_factory=list)
    image_available: bool = False


class EvidenceSource(BaseModel):
    model_config = ConfigDict(frozen=True)
    tenant_id: UUID
    chunk_id: UUID
    source_chunk_ids: list[UUID] = Field(default_factory=list)
    parse_run_id: UUID
    provenance: Provenance
    retrieval_text: str
    evidence_text: str | None = None
    text: str
    spans: list[SourceSpan]
    hierarchy: list[SourceSpan] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    question: dict[str, Any] | None = None


class EvidenceBlock(BaseModel):
    evidence_id: UUID
    anchor_chunk_id: UUID
    source_chunk_ids: list[UUID]
    source_element_ids: list[UUID]
    document_id: UUID
    document_version_id: UUID
    chunk_run_id: UUID
    parse_run_id: UUID
    document_title: str
    source_type: str
    authority_level: str
    chunk_type: str
    pages: list[int]
    hierarchy: list[SourceSpan]
    source_spans: list[SourceSpan]
    text: str
    representation: Literal["m3-source-with-structural-labels-v1", "source-spans-v1"]
    artifacts: list[ArtifactRef]
    question: dict[str, Any] | None = None
    expansion_reason: str
    context_reasons: list[str] = Field(default_factory=list)
    token_count: int
    requires_visual_evidence: bool


class EvidenceSet(BaseModel):
    query_hash: str
    retrieval_trace: dict[str, Any]
    reranking_trace: dict[str, Any]
    anchors: list[UUID]
    expansions: list[UUID]
    evidence_blocks: list[EvidenceBlock]
    total_tokens: int
    requires_visual_evidence: bool
    warnings: list[str]
    duplicates_removed: int
    answering_enabled: Literal[False] = False
