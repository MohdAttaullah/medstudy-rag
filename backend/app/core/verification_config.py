"""M8 claim-verification policies.

Query-side only: no stored entity, no migration, no ingestion state change. Every policy is frozen
and fingerprinted so a trace can reconstruct exactly why an answer passed or abstained.
"""

from typing import Literal

from pydantic import Field

from app.core.reranking_config import Policy


class ClaimExtractionConfig(Policy):
    version: Literal["claim-extraction-m8-v1"] = "claim-extraction-m8-v1"
    # Deterministic. A model deciding what its own answer claimed would be marking its own work,
    # and the split is exactly where an unsupported proposition can be hidden.
    method: Literal["DETERMINISTIC_SEGMENTATION"] = "DETERMINISTIC_SEGMENTATION"
    # Extraction runs over the answer a reader would see, not only over the claims the generator
    # chose to declare, so prose the generator never bound to evidence cannot pass unnoticed.
    source: Literal["DRAFT_ANSWER_TEXT"] = "DRAFT_ANSWER_TEXT"
    split_coordinated_clauses: bool = True
    # Below this many content terms a fragment carries no verifiable proposition — "Therefore," and
    # "Based on the evidence," are connective, not medical.
    min_material_terms: int = Field(default=3, ge=1, le=20)
    max_claims: int = Field(default=60, ge=1, le=300)


class ClaimVerificationConfig(Policy):
    version: Literal["claim-verification-m8-v1"] = "claim-verification-m8-v1"
    # Deterministic checks run first and are final: a model is never asked whether a nonexistent
    # citation, a wrong dose or a reversed negation is acceptable.
    deterministic_checks_are_final: Literal[True] = True
    require_citation_for_material_claims: bool = True
    # Numbers, units and qualifiers are where a fluent answer does the most damage, so each has an
    # explicit deterministic check rather than being left to a semantic judgement.
    check_numeric_agreement: bool = True
    check_negation_agreement: bool = True
    check_certainty_overstatement: bool = True
    # A model verdict can only ever fail a claim that deterministic checks passed; it can never
    # rescue one they failed.
    semantic_verification_enabled: bool = True
    verifier_required: bool = True


class ContradictionConfig(Policy):
    version: Literal["contradiction-m8-v1"] = "contradiction-m8-v1"
    # M7 could only see disagreement between whole evidence blocks. M8 additionally asks whether a
    # claim is contradicted by evidence that was retrieved and retained but not cited — the case a
    # generator creates by citing only the source that agrees with it.
    check_uncited_retained_evidence: bool = True
    check_authoritative_disagreement: bool = True
    # Assessment material never outranks a reference source, and rank never decides which of two
    # sources is true.
    assessment_cannot_override_reference: Literal[True] = True
    rank_may_break_ties: Literal[False] = False


class RepairConfig(Policy):
    version: Literal["repair-m8-v1"] = "repair-m8-v1"
    enabled: bool = True
    # Pinned by type. Repair exists to remove unsupported content once, not to retry until
    # something passes; a loop would eventually produce a draft that survives by luck.
    max_attempts: Literal[1] = 1
    # A repair may only narrow the answer. Widening retrieval or adding evidence would make the
    # repaired draft answer a different question from the one that was gated.
    may_widen_evidence: Literal[False] = False


class FinalVerificationConfig(Policy):
    version: Literal["final-verification-m8-v1"] = "final-verification-m8-v1"
    # An answer is released only when every material claim survives every check. There is no
    # partial-credit release and no score to trade off against coverage.
    require_all_material_claims_supported: Literal[True] = True
    require_deterministic_citation_success: Literal[True] = True
    contradiction_abstains: Literal[True] = True
    verifier_failure_abstains: Literal[True] = True
    # M7 abstains on questions needing visual interpretation; M8 must not become a way around that.
    visual_claims_abstain: Literal[True] = True
