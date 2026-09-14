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


class EvidenceWarning(BaseModel):
    """One thing that went wrong while assembling evidence, with enough structure to judge it.

    The gate has to distinguish "the evidence I selected is incomplete" from "a candidate I did
    not use did not fit". Parsed out of a `"CODE:uuid"` string those are indistinguishable, and
    treating every warning alike made a 932-page textbook unanswerable while its defining passage
    sat at rank one. See ADR-019.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    #: The candidate the warning is about, when it is about one at all. Retrieval-level integrity
    #: warnings concern the whole query and name nothing.
    chunk_id: UUID | None = None
    #: ANCHOR for a reranker-selected candidate, EXPANSION for optional surrounding context,
    #: RETRIEVAL for a first-stage condition that belongs to no tier.
    tier: Literal["ANCHOR", "EXPANSION", "RETRIEVAL"] = "RETRIEVAL"
    #: Whether the reranker chose this candidate as an anchor. True exactly when tier is ANCHOR.
    selected_by_reranker: bool = False
    #: Whether an admitted anchor actually needed this, as opposed to it being optional context.
    required_dependency: bool = False
    #: The anchor this warning is about, when it is about one. A dropped parent names the parent
    #: in `chunk_id`, so without this the anchor it was meant to complete could not be identified.
    anchor_chunk_id: UUID | None = None

    def render(self) -> str:
        """The historical string form, kept so existing readers and the UI are unaffected."""
        return f"{self.code}:{self.chunk_id}" if self.chunk_id else self.code


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
    #: For a block whose spans were trimmed: whether every trimmed region is carried by
    #: another block in the same EvidenceSet. Trimming removes duplication rather than
    #: truncating, so assembly normally proves this true — and when it is, a partial block is
    #: missing nothing. The gate needs that distinction and cannot recompute it from the
    #: retained spans alone, so assembly records it.
    #:
    #: Defaults to False: a block that does not say has not proved coverage, and an unproved
    #: partial anchor must be treated as materially incomplete. See ADR-019.
    trimmed_text_present_elsewhere: bool = False
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
    #: Human-readable form, unchanged, so diagnostics and the evidence inspector keep working.
    warnings: list[str]
    #: The same warnings with the structure the sufficiency gate reasons over. Additive: the
    #: string list above stays the display surface.
    warning_details: list[EvidenceWarning] = Field(default_factory=list)
    #: Reranker-selected anchors whose required context could not be satisfied. They are absent
    #: from `evidence_blocks` and `anchors`, so they cannot be rendered, cited, claimed against or
    #: counted — unusable evidence is removed rather than flagged. See ADR-020.
    excluded_anchors: list[UUID] = Field(default_factory=list)
    #: The blocks belonging to those anchors, kept only so conflict detection still sees the full
    #: assembled set. Nothing downstream of the gate reads this: generation, citation binding and
    #: verification all consume `evidence_blocks`.
    excluded_blocks: list[EvidenceBlock] = Field(default_factory=list)
    duplicates_removed: int
    answering_enabled: Literal[False] = False
