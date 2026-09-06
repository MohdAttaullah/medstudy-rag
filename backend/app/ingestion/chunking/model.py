"""Immutable chunker input and output contracts, independent of ORM and parser libraries."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourcePage(Frozen):
    id: UUID
    number: int
    width: float
    height: float


class SourceElement(Frozen):
    id: UUID
    page_id: UUID | None
    page_number: int | None
    reading_order: int
    parent_id: UUID | None = None
    kind: str = "PARAGRAPH"
    text: str = ""
    raw_text: str | None = None
    bbox: tuple[float, float, float, float] | None = None


class SourceArtifact(Frozen):
    id: UUID
    element_id: UUID
    kind: str
    page_number: int | None = None
    caption_id: UUID | None = None
    caption: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    related_ids: tuple[UUID, ...] = ()


class ChunkInput(Frozen):
    document_id: UUID
    version_id: UUID
    parse_run_id: UUID
    title: str
    source_type: str
    authority_level: str
    edition: str | None = None
    publication_year: int | None = None
    pages: tuple[SourcePage, ...]
    elements: tuple[SourceElement, ...]
    artifacts: tuple[SourceArtifact, ...] = ()


class Span(Frozen):
    element_id: UUID
    start: int = 0
    end: int
    role: str = "SOURCE"


class QuestionOptionDraft(Frozen):
    label: str
    text: str
    ordinal: int


class QuestionDraft(Frozen):
    key: str
    number: str | None
    text: str
    question_type: str
    options: tuple[QuestionOptionDraft, ...]
    explicit_answer: str | None
    explanation: str | None
    extraction_status: str
    source_spans: tuple[Span, ...]
    authority: dict[str, Any]
    structure_inferred: bool = True
    answer_inferred: bool = False


class DraftChunk(Frozen):
    key: str
    kind: str
    sequence: int
    source_text: str
    retrieval_text: str
    token_count: int
    retrieval_token_count: int
    spans: tuple[Span, ...]
    artifact_ids: tuple[UUID, ...] = ()
    hierarchy: tuple[UUID, ...] = ()
    parent_key: str | None = None
    question_key: str | None = None
    page_start: int | None
    page_end: int | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Finding(Frozen):
    severity: str
    code: str
    message: str
    chunk_key: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ChunkDataset(Frozen):
    chunks: tuple[DraftChunk, ...]
    questions: tuple[QuestionDraft, ...] = ()
    findings: tuple[Finding, ...] = ()
    excluded_element_ids: tuple[UUID, ...] = ()
    input_fingerprint: str
