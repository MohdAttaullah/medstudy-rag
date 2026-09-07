"""M7 sufficiency and grounding policies.

Query-side only: no stored entity, no migration, no ingestion state change. Every policy here is
frozen and fingerprinted so a trace can reconstruct exactly which rules produced a decision.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.core.reranking_config import Policy

QuestionKind = Literal[
    "ORDINARY_FACTUAL",
    "TABLE_DEPENDENT",
    "FORMULA_DEPENDENT",
    "FIGURE_DEPENDENT",
    "ASSESSMENT",
]


class EvidenceRequirement(Policy):
    """What one kind of question must have in its EvidenceSet before generation is attempted.

    These are structural requirements over the evidence, deliberately not a score. No retrieval,
    fusion or reranker number appears anywhere in this model; see ADR-012.
    """

    min_supporting_blocks: int = Field(default=1, ge=1, le=20)
    min_independent_sources: int = Field(default=1, ge=1, le=10)
    # Assessment material is not automatically medical truth, so a question answered only by a
    # question bank or answer key has no medical source behind it.
    require_non_assessment_source: bool = True
    require_table_structure: bool = False
    require_formula_source: bool = False
    require_visual_interpretation: bool = False


class SufficiencyConfig(Policy):
    version: Literal["sufficiency-m7-v1"] = "sufficiency-m7-v1"
    decision_basis: Literal["DETERMINISTIC_SIGNALS"] = "DETERMINISTIC_SIGNALS"
    # Pinned false by type. M6 observed that CrossEncoder logits separated its synthetic positives
    # from its negatives; that is an engineering observation on 24 queries and is not calibrated
    # evidence sufficiency. No retrieval, BM25, dense, RRF or reranker score may enter this gate.
    retrieval_scores_permitted: Literal[False] = False
    # M7 has no approved vision-analysis path, so a question that genuinely needs the original
    # image interpreted cannot be answered from a caption or a structural label.
    vision_analysis_available: Literal[False] = False

    ordinary: EvidenceRequirement = EvidenceRequirement()
    table: EvidenceRequirement = EvidenceRequirement(require_table_structure=True)
    formula: EvidenceRequirement = EvidenceRequirement(require_formula_source=True)
    figure: EvidenceRequirement = EvidenceRequirement(require_visual_interpretation=True)
    assessment: EvidenceRequirement = EvidenceRequirement(require_non_assessment_source=True)

    # An evidence block omitted because it did not fit the budget is missing source, not absent
    # source; treating it as a non-event would let the generator answer from a truncated corpus.
    budget_omission_is_insufficient: bool = True
    # A block whose text was assembled from partial spans is not a complete source record.
    incomplete_context_is_insufficient: bool = True
    conflict_policy: Literal["CONFLICTING", "REPORT_ONLY"] = "CONFLICTING"

    def for_kind(self, kind: QuestionKind) -> EvidenceRequirement:
        return {
            "ORDINARY_FACTUAL": self.ordinary,
            "TABLE_DEPENDENT": self.table,
            "FORMULA_DEPENDENT": self.formula,
            "FIGURE_DEPENDENT": self.figure,
            "ASSESSMENT": self.assessment,
        }[kind]


class GroundingConfig(Policy):
    version: Literal["grounding-m7-v1"] = "grounding-m7-v1"
    prompt_version: Literal["grounded-draft-v1"] = "grounded-draft-v1"
    schema_version: Literal["grounded-draft-schema-v1"] = "grounded-draft-schema-v1"
    # All pinned by type: the provider sees the EvidenceSet and nothing else, and its own
    # pretrained knowledge is never a permitted source for a medical statement.
    pretrained_knowledge_is_evidence: Literal[False] = False
    corpus_access: Literal["EVIDENCE_SET_ONLY"] = "EVIDENCE_SET_ONLY"
    provider_tools_enabled: Literal[False] = False
    provider_web_search_enabled: Literal[False] = False
    max_evidence_blocks: int = Field(default=20, ge=1, le=40)
    max_question_chars: int = Field(default=2000, ge=1, le=8000)
    max_answer_chars: int = Field(default=8000, ge=1, le=32000)
    max_claims: int = Field(default=40, ge=1, le=200)


class ProviderConfig(Policy):
    version: Literal["provider-m7-v1"] = "provider-m7-v1"
    request_timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_output_tokens: int = Field(default=2048, ge=64, le=16384)
    # Omitted from the request unless explicitly set. Several current models accept only their
    # default and reject any explicit value, and sending one anyway fails the whole request. Just
    # as important: the value reaching the provider is recorded in the draft's provenance, so a
    # temperature we did not actually send must never be written there. None means "provider
    # default"; determinism was never guaranteed by a temperature setting in any case.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    # One attempt. A retry that silently reached a different provider or model would make the
    # recorded provenance of a medical draft untrue, which is worse than a failed request.
    max_attempts: Literal[1] = 1
    fallback_policy: Literal["NONE"] = "NONE"
    # Deliberately empty by default. Every vendor endpoint, header and wire version lives in that
    # vendor's adapter; naming one here would put provider detail back in core configuration,
    # which is the boundary ADR-005 exists to hold. These exist only to point a test double or a
    # gateway at a different host.
    openai_base_url: str = ""
    anthropic_base_url: str = ""

    @model_validator(mode="after")
    def deterministic_by_default(self) -> Self:
        if self.temperature not in (None, 0.0) and self.max_attempts != 1:
            raise ValueError("Sampled generation must not be retried without traceability")
        return self
