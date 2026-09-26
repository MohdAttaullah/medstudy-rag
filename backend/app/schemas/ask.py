"""The public Ask contract.

This is the only response in the system that may carry a medical answer, and the rule that governs
it is enforced here rather than left to the caller: `answer` exists if and only if the outcome is
`VERIFIED`, and `verified` agrees with both. A response cannot be *constructed* in a shape that
would let an unverified draft reach a reader, so no route, serializer or future refactor can leak
one by omission.

There is deliberately no field for the M7 draft, the sufficiency signals, the verifier's reasoning
or any provider internals. Those exist, and authorized reviewers can see them on the inspector
routes; they have no place in the response a patient-facing surface renders.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.retrieval import SearchFilters

AskOutcome = Literal[
    "VERIFIED",
    "INSUFFICIENT_EVIDENCE",
    "CONFLICTING_EVIDENCE",
    "UNVERIFIED",
    "FAILED",
    # Refused on intent, before any index was searched or any provider called: the reader asked
    # for individualized clinical advice, which this educational system does not give. Carries no
    # answer and no citations, because none were ever produced. See ADR-021.
    "OUT_OF_SCOPE",
]


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None
    # Retried submissions reuse the key and return the stored turn instead of spending another
    # provider call.
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)
    filters: SearchFilters | None = None
    # Deliberately absent: tenant id, evidence ids, verification state, provider, model, prompt,
    # source-authority overrides. Tenant and corpus identity are server-owned, and a client that
    # could assert `verified` would be asserting the one thing this milestone exists to establish.


class CitationSpan(BaseModel):
    """A real recorded region, or an honest absence of one."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    element_id: UUID
    page: int | None = None
    start: int
    end: int
    role: str
    # None where M2 recorded no geometry. The viewer falls back to the page rather than inventing a
    # box.
    bbox: tuple[float | None, float | None, float | None, float | None] | None = None


class CitationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: UUID
    kind: str
    row_indexes: list[int] = Field(default_factory=list)
    header_rows: list[int] = Field(default_factory=list)
    image_available: bool = False


class AskCitation(BaseModel):
    """One verified source behind the answer, carrying what it needs to be opened and judged."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    citation_id: UUID
    ordinal: int
    document_id: UUID
    document_version_id: UUID
    parse_run_id: UUID
    chunk_run_id: UUID
    document_title: str
    source_type: str
    # Kept on every citation so assessment material can never be presented as a reference source.
    authority_level: str
    chunk_type: str
    pages: list[int]
    spans: list[CitationSpan]
    artifacts: list[CitationArtifact]
    cited_text: str


class AskFigure(BaseModel):
    """A source figure an answer may display, and the provenance that earned it a place.

    It is not evidence. `linked_by` records which provenance link put it here — the citation is
    that figure, or the citation's verified text names it — so a reader can check the reason
    rather than take it on trust. No image is interpreted anywhere in this system.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    figure_id: UUID
    document_id: UUID
    document_version_id: UUID
    parse_run_id: UUID
    document_title: str
    page: int | None
    caption: str | None
    label: str | None
    linked_by: Literal["CITED_EVIDENCE", "CITED_TEXT_REFERENCE"]
    citation_ids: list[UUID]


class AskSource(BaseModel):
    """One document behind the answer. Retrieval candidates that supported nothing never appear."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    document_id: UUID
    document_version_id: UUID
    parse_run_id: UUID
    title: str
    source_type: str
    authority_level: str
    pages: list[int]
    citation_ids: list[UUID]


class AskClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    citation_ids: list[UUID]


class StageTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: str
    duration_ms: float


class AskResponse(BaseModel):
    """The one response that may contain an answer, and only under the outcome that earns it."""

    model_config = ConfigDict(extra="forbid")
    correlation_id: UUID
    conversation_id: UUID
    turn_id: UUID
    question: str
    outcome: AskOutcome
    # True only alongside a VERIFIED outcome and a present answer; see the validator below.
    verified: bool = False
    # This response *may* carry an answer — unlike every M5–M8 response, which pins the flag false.
    # It is a statement about this contract, not a global switch that turns answering on.
    answering_enabled: Literal[True] = True
    answer: str | None = None
    claims: list[AskClaim] = Field(default_factory=list)
    citations: list[AskCitation] = Field(default_factory=list)
    sources: list[AskSource] = Field(default_factory=list)
    #: Source figures the citations link to. Supplementary material, never evidence: nothing here
    #: was interpreted, and the validator below keeps them out of every unverified outcome.
    figures: list[AskFigure] = Field(default_factory=list)
    # A plain sentence for the reader, chosen from a fixed set per outcome. Never provider prose,
    # never a verifier's rationale, never an internal code path.
    message: str
    reason_codes: list[str] = Field(default_factory=list)
    stages: list[StageTiming] = Field(default_factory=list)
    created_at: str

    @model_validator(mode="after")
    def answer_only_when_verified(self) -> Self:
        """Make the display rule unrepresentable to violate.

        Every non-verified outcome — insufficient evidence, conflict, failed verification, provider
        failure — must arrive with no answer text at all. Returning one in a failure object is the
        exact shape that lets an interface render an unverified draft by mistake.
        """
        verified = self.outcome == "VERIFIED"
        if verified != (self.answer is not None):
            raise ValueError("An answer exists if and only if the outcome is VERIFIED")
        if verified != self.verified:
            raise ValueError("`verified` must agree with the outcome")
        if not verified and (self.claims or self.citations or self.sources or self.figures):
            raise ValueError("Only a verified answer carries claims, citations, sources or figures")
        return self


class ConversationTurnView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    turn_id: UUID
    sequence_number: int
    question: str
    outcome: AskOutcome
    verified: bool
    answer: str | None
    message: str
    reason_codes: list[str]
    citations: list[AskCitation]
    sources: list[AskSource]
    figures: list[AskFigure] = Field(default_factory=list)
    created_at: str

    @model_validator(mode="after")
    def stored_answer_only_when_verified(self) -> Self:
        # The same rule on the way out of the database as on the way in.
        if (self.outcome == "VERIFIED") != (self.answer is not None):
            raise ValueError("A stored answer exists if and only if the turn was verified")
        return self


class ConversationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: UUID
    title: str
    created_at: str
    updated_at: str
    turns: list[ConversationTurnView] = Field(default_factory=list)


class ConversationSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: UUID
    title: str
    turn_count: int
    verified_turns: int
    created_at: str
    updated_at: str
