"""Typed, inspectable evidence-sufficiency contracts.

The decision is a structured artifact, not an opinion: every status is accompanied by the reason
codes that produced it and by the signals those codes were read from, so a reviewer can reconstruct
the decision without rerunning it.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import DomainError

SufficiencyStatus = Literal["SUFFICIENT", "INSUFFICIENT", "CONFLICTING"]

ReasonCode = Literal[
    # Supporting
    "SUPPORTED_BY_SOURCE_EVIDENCE",
    "SUPPORTED_BY_INDEPENDENT_SOURCES",
    "SUPPORTED_BY_NON_ASSESSMENT_SOURCE",
    "REQUIRED_ARTIFACT_PRESENT",
    # Insufficiency
    "NO_EVIDENCE",
    "INSUFFICIENT_SUPPORTING_BLOCKS",
    "INSUFFICIENT_INDEPENDENT_SOURCES",
    "ASSESSMENT_ONLY_EVIDENCE",
    "TABLE_STRUCTURE_INCOMPLETE",
    "FORMULA_SOURCE_MISSING",
    "VISUAL_INTERPRETATION_UNAVAILABLE",
    "EVIDENCE_BUDGET_OMISSION",
    "CONTEXT_INCOMPLETE",
    "RETRIEVAL_WARNING_PRESENT",
    # A selected anchor is a mid-sentence fragment whose completing parent was not admitted.
    "REQUIRED_CONTEXT_MISSING",
    # The provider declared that the supplied evidence does not address the question. A semantic
    # abstention, not a technical failure: the corpus was searched and does not cover this.
    # See ADR-022.
    "EVIDENCE_DOES_NOT_ADDRESS_QUESTION",
    # Refused before retrieval: the reader asked for individualized clinical advice, which this
    # educational system does not give at any evidence level. See ADR-021.
    "PERSONAL_MEDICAL_ADVICE_REQUESTED",
    # Advisory
    # Optional surrounding context the assembler declined. Reported so the omission stays
    # visible; never a reason to refuse generation. See ADR-019.
    "ADVISORY_CONTEXT_OMISSION",
    # Conflict
    "ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE",
    "INDEPENDENT_SOURCE_VALUE_CONFLICT",
]

# Source types that are assessment material. A key in a question bank records what an examiner
# marked correct; it is not, on its own, a medical reference. Enforced at ingestion too, where
# these source types are already forbidden from claiming REFERENCE or HIGH authority.
ASSESSMENT_SOURCE_TYPES = frozenset({"QUESTION_BANK", "QUESTION_PAPER", "ANSWER_KEY"})
ASSESSMENT_AUTHORITY = frozenset({"ASSESSMENT"})
REFERENCE_AUTHORITY = frozenset({"REFERENCE", "HIGH"})


class SufficiencyError(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(code, code.replace("_", " ").capitalize() + ".", 409)


class EvaluatedSignal(BaseModel):
    """One measured property of the EvidenceSet, with the requirement it was compared against."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    value: Any
    required: Any = None
    satisfied: bool


class EvidenceConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE", "INDEPENDENT_SOURCE_VALUE_CONFLICT"]
    description: str
    evidence_ids: list[UUID]
    document_version_ids: list[UUID]
    detail: dict[str, Any] = Field(default_factory=dict)


class SufficiencyDecision(BaseModel):
    """The gate's complete output. `SUFFICIENT` is the only status that permits generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: SufficiencyStatus
    question_kind: str
    reason_codes: list[ReasonCode]
    evaluated_signals: list[EvaluatedSignal]
    supporting_evidence_ids: list[UUID]
    conflicting_evidence_ids: list[UUID]
    conflicts: list[EvidenceConflict]
    missing_requirements: list[str]
    policy_version: str
    policy_fingerprint: str
    # Deliberately absent: any percentage, probability or score. M7 establishes no calibrated
    # notion of medical confidence and must not imply one.

    @property
    def generation_permitted(self) -> bool:
        return self.status == "SUFFICIENT"
