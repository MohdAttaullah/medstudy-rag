"""M8 claim-verification contracts.

An M7 draft is untrusted output. These types describe what had to be true for it to stop being
untrusted, in a form a reviewer can re-read: every material claim, what it cited, what each check
concluded and which check refused. `verified=true` exists nowhere except behind a PASS.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import DomainError
from app.generation.grounding.model import GroundedDraft, ProviderSpec

Verdict = Literal[
    "SUPPORTED",
    "UNSUPPORTED",
    "CONTRADICTED",
    "INSUFFICIENT_EVIDENCE",
    "UNVERIFIABLE",
]

Outcome = Literal["PASS", "REGENERATE_ONCE", "ABSTAIN"]

AbstentionReason = Literal[
    "UNSUPPORTED_CLAIM",
    "CONTRADICTED_CLAIM",
    "EVIDENCE_CONFLICT",
    "CITATION_INVALID",
    "VERIFICATION_UNAVAILABLE",
    "REPAIR_FAILED",
]

ClaimType = Literal[
    "FACTUAL",
    "NUMERIC",
    "NEGATED",
    "QUALIFIED",
    "TABLE_DERIVED",
    "FORMULA_DERIVED",
    "VISUAL_DEPENDENT",
    "ASSESSMENT_DERIVED",
    "NON_MATERIAL",
]

ReasonCode = Literal[
    # Deterministic failures. These are final: a model is never asked to overrule one.
    "CLAIM_NOT_CITED",
    "UNKNOWN_CITATION",
    "PROVENANCE_UNRESOLVED",
    "TENANT_SCOPE_VIOLATION",
    "NUMERIC_MISMATCH",
    "UNIT_MISMATCH",
    "NEGATION_REVERSED",
    "OVERSTATED_CERTAINTY",
    "VISUAL_INTERPRETATION_REQUIRED",
    # Semantic verdicts.
    "SEMANTICALLY_UNSUPPORTED",
    "SEMANTICALLY_CONTRADICTED",
    "SEMANTIC_EVIDENCE_INSUFFICIENT",
    "VERIFIER_FAILED",
    "VERIFIER_MALFORMED_OUTPUT",
    "VERIFIER_UNKNOWN_EVIDENCE",
    # Evidence-level disagreement.
    "CONTRADICTED_BY_RETAINED_EVIDENCE",
    "AUTHORITATIVE_SOURCES_DISAGREE",
    "ASSESSMENT_CONTRADICTS_REFERENCE",
    # Success.
    "SUPPORTED_BY_CITED_EVIDENCE",
    "NON_MATERIAL_CLAIM",
]

# A defect the generator could remove by rewriting from the same evidence. Everything else is a
# property of the corpus or of the request, which no amount of rewriting changes.
REPAIRABLE: frozenset[str] = frozenset(
    {
        "CLAIM_NOT_CITED",
        "NUMERIC_MISMATCH",
        "UNIT_MISMATCH",
        "NEGATION_REVERSED",
        "OVERSTATED_CERTAINTY",
        "SEMANTICALLY_UNSUPPORTED",
        "SEMANTIC_EVIDENCE_INSUFFICIENT",
    }
)


class VerificationError(DomainError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(
            code,
            detail or code.replace("_", " ").capitalize() + ".",
            503 if code.startswith("VERIFIER") else 409,
        )


class Claim(BaseModel):
    """One atomic proposition taken from the answer a reader would actually see."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: UUID
    text: str
    # Character span within the draft answer, so a finding points at the words that caused it.
    start: int
    end: int
    claim_type: ClaimType
    material: bool
    # Inherited from the declared draft claim covering this proposition. Empty on a material claim
    # is itself a failure: the generator wrote something it never bound to evidence.
    cited_evidence_ids: list[UUID] = Field(default_factory=list)
    # The sentence this proposition was taken from, verbatim. It exists so a verifier can resolve
    # what "it" or an elided subject refers to; it is never evidence and is never verified itself.
    context: str = ""


class ClaimVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: UUID
    claim_text: str
    claim_type: ClaimType
    material: bool
    verdict: Verdict
    reason_codes: list[ReasonCode]
    supporting_evidence_ids: list[UUID] = Field(default_factory=list)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list)
    # What the deterministic layer measured, kept so a verdict can be re-read rather than trusted.
    detail: dict[str, Any] = Field(default_factory=dict)
    # Deliberately absent: any score or percentage. M8 establishes no calibrated confidence.

    @property
    def failed(self) -> bool:
        return self.material and self.verdict != "SUPPORTED"


class ContradictionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[
        "CONTRADICTED_BY_RETAINED_EVIDENCE",
        "AUTHORITATIVE_SOURCES_DISAGREE",
        "ASSESSMENT_CONTRADICTS_REFERENCE",
    ]
    description: str
    claim_ids: list[UUID] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)
    document_version_ids: list[UUID] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class VerifierSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verifier: Literal["model", "deterministic-fake"]
    provider: str
    model_id: str
    prompt_version: str
    schema_version: str
    # True only when the verifier reached a different provider *or* model from the generator. When
    # false, a shared blind spot can pass both stages, and the report must say so.
    independent_of_generator: bool


class VerificationReport(BaseModel):
    """Everything the final policy read, whether it passed or abstained."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: Outcome
    claims: list[Claim]
    verifications: list[ClaimVerification]
    contradictions: list[ContradictionFinding]
    failed_reason_codes: list[ReasonCode]
    material_claims: int
    supported_claims: int
    repair_count: int
    verifier: VerifierSpec | None
    claim_extraction_fingerprint: str
    claim_verification_fingerprint: str
    contradiction_fingerprint: str
    repair_fingerprint: str
    final_policy_fingerprint: str
    durations_ms: dict[str, float] = Field(default_factory=dict)


class VerifiedAnswer(BaseModel):
    """Produced only behind a PASS. The one place in the system where `verified` is true."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    answer_id: UUID
    verified: Literal[True] = True
    verification_status: Literal["VERIFIED"] = "VERIFIED"
    answer: str
    claims: list[ClaimVerification]
    cited_evidence_ids: list[UUID]
    query_hash: str
    draft: GroundedDraft
    generator: ProviderSpec
    verifier: VerifierSpec
    repair_count: int


class VerificationAbstention(BaseModel):
    """Returned instead of an answer. Abstention is a successful outcome, not an error."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    abstained: Literal[True] = True
    verified: Literal[False] = False
    reason: AbstentionReason
    reason_codes: list[ReasonCode]
    message: str
    unsupported_claim_ids: list[UUID] = Field(default_factory=list)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list)
