"""Bounded, tenant-authorized M3 inspection contracts. No storage keys or leases."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.chunking_config import ChunkingConfig
from app.schemas.documents import ORMView
from app.schemas.parsing import ElementView


class RechunkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False
    config: ChunkingConfig | None = None


class ChunkRunView(ORMView):
    id: UUID
    document_id: UUID
    document_version_id: UUID
    parse_run_id: UUID
    ingestion_job_id: UUID
    generation: int
    status: str
    validation_result: str | None
    is_active: bool
    chunker_name: str
    chunker_version: str
    configuration_version: str
    policy_fingerprint: str
    config_snapshot: dict[str, Any]
    tokenizer_name: str
    tokenizer_version: str
    input_fingerprint: str | None
    metrics: dict[str, Any]
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    created_at: datetime


class ChunkFindingView(ORMView):
    """A validation finding attached to one chunk, carried with the chunk so its state is visible
    in a list without a second request."""

    severity: str
    code: str
    message: str
    details: dict[str, Any]


class ChunkView(ORMView):
    id: UUID
    chunk_run_id: UUID
    parse_run_id: UUID
    parent_chunk_id: UUID | None
    question_id: UUID | None
    chunk_type: str
    sequence_number: int
    raw_text: str
    normalized_text: str
    retrieval_text: str
    token_count: int
    retrieval_token_count: int
    page_start: int | None
    page_end: int | None
    chunk_hash: str
    chunk_metadata: dict[str, Any]
    findings: list[ChunkFindingView] = Field(default_factory=list)


class SourceView(BaseModel):
    element: ElementView
    position: int
    start_offset: int
    end_offset: int
    role: str


class ArtifactLinkView(ORMView):
    table_id: UUID | None
    figure_id: UUID | None
    formula_id: UUID | None


class ChunkDetailView(ChunkView):
    artifacts: list[ArtifactLinkView] = Field(default_factory=list)
    next_sibling_ids: list[UUID] = Field(default_factory=list)


class OptionView(ORMView):
    ordinal: int
    label: str
    text: str


class QuestionView(ORMView):
    id: UUID
    question_hash: str
    question_number: str | None
    question_text: str
    question_type: str
    explicit_answer: str | None
    explanation: str | None
    extraction_status: str
    structure_inferred: bool
    answer_inferred: bool
    authority: dict[str, Any]
    page_start: int | None
    page_end: int | None
    options: list[OptionView] = Field(default_factory=list)


class FindingView(ORMView):
    id: UUID
    chunk_id: UUID | None
    severity: str
    code: str
    message: str
    details: dict[str, Any]


class ChunkReviewSummary(BaseModel):
    """Counts for the inspector's views, so a reviewer can see where to start before opening any.

    Blocking means CRITICAL or ERROR — the severities that stop a dataset from becoming active.
    A chunk is counted once, under its worst finding.
    """

    chunks: int
    questions: int
    findings: int
    blocking_findings: int
    warning_findings: int
    info_findings: int
    blocking_chunks: int
    warning_chunks: int
    clean_chunks: int
    # Findings about the dataset as a whole rather than one chunk (for example a coverage ratio).
    dataset_findings: int
