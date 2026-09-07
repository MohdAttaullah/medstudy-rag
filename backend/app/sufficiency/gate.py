"""The Evidence Sufficiency Gate.

Runs before generation and decides whether generation may be attempted at all. The decision is made
from structural properties of the EvidenceSet — how many independent sources support the question,
whether the artifact the question depends on is actually present and complete, whether anything was
dropped for budget, whether the sources disagree — and never from a retrieval, fusion or reranker
score. See ADR-012 for why a single number cannot stand in for any of this.
"""

from uuid import UUID

from app.core.generation_config import EvidenceRequirement, SufficiencyConfig
from app.evidence.model import EvidenceBlock, EvidenceSet
from app.sufficiency import conflicts as conflict_detection
from app.sufficiency.model import (
    ASSESSMENT_AUTHORITY,
    ASSESSMENT_SOURCE_TYPES,
    EvaluatedSignal,
    EvidenceConflict,
    ReasonCode,
    SufficiencyDecision,
)
from app.sufficiency.question import classify

BUDGET_WARNING = "CONTEXT_BUDGET_EXCEEDED"
ANCHOR = "RERANKED_ANCHOR"


class SufficiencyGate:
    def __init__(self, config: SufficiencyConfig) -> None:
        self.config = config

    def evaluate(self, question: str, evidence: EvidenceSet) -> SufficiencyDecision:
        blocks = list(evidence.evidence_blocks)
        # Expanded context supports its anchor; it is not independent support for the question.
        anchors = [b for b in blocks if b.expansion_reason == ANCHOR]
        kind = classify(question, anchors)
        requirement = self.config.for_kind(kind)

        if not blocks:
            return self._decide("INSUFFICIENT", kind, ["NO_EVIDENCE"], [], [], [], ["evidence"], [])

        signals: list[EvaluatedSignal] = []
        reasons: list[ReasonCode] = []
        missing: list[str] = []

        supporting = [b for b in anchors if not self._assessment(b)] or anchors
        versions = {b.document_version_id for b in anchors}

        self._count_checks(anchors, versions, requirement, signals, reasons, missing)
        self._authority_checks(anchors, requirement, signals, reasons, missing)
        self._artifact_checks(anchors, requirement, signals, reasons, missing)
        self._completeness_checks(evidence, blocks, signals, reasons, missing)

        found = conflict_detection.detect(blocks)
        signals.append(
            EvaluatedSignal(
                name="evidence_conflicts", value=len(found), required=0, satisfied=not found
            )
        )
        conflicting: list[UUID] = []
        for conflict in found:
            conflicting.extend(conflict.evidence_ids)
            code: ReasonCode = (
                "ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE"
                if conflict.kind == "ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE"
                else "INDEPENDENT_SOURCE_VALUE_CONFLICT"
            )
            if code not in reasons:
                reasons.append(code)

        # Precedence: a detected disagreement is the most specific safety finding, so it names the
        # status. Every insufficiency reason is still reported, so nothing is hidden by the choice.
        if found and self.config.conflict_policy == "CONFLICTING":
            status = "CONFLICTING"
        elif missing:
            status = "INSUFFICIENT"
        else:
            status = "SUFFICIENT"
            reasons.append("SUPPORTED_BY_SOURCE_EVIDENCE")

        return self._decide(
            status,
            kind,
            reasons,
            [b.evidence_id for b in supporting],
            sorted(set(conflicting), key=str),
            found,
            missing,
            signals,
        )

    @staticmethod
    def _assessment(block: EvidenceBlock) -> bool:
        return (
            block.source_type in ASSESSMENT_SOURCE_TYPES
            or block.authority_level in ASSESSMENT_AUTHORITY
        )

    def _count_checks(
        self,
        anchors: list[EvidenceBlock],
        versions: set[UUID],
        requirement: EvidenceRequirement,
        signals: list[EvaluatedSignal],
        reasons: list[ReasonCode],
        missing: list[str],
    ) -> None:
        enough_blocks = len(anchors) >= requirement.min_supporting_blocks
        signals.append(
            EvaluatedSignal(
                name="supporting_blocks",
                value=len(anchors),
                required=requirement.min_supporting_blocks,
                satisfied=enough_blocks,
            )
        )
        if not enough_blocks:
            reasons.append("INSUFFICIENT_SUPPORTING_BLOCKS")
            missing.append("supporting_blocks")

        enough_sources = len(versions) >= requirement.min_independent_sources
        signals.append(
            EvaluatedSignal(
                name="independent_sources",
                value=len(versions),
                required=requirement.min_independent_sources,
                satisfied=enough_sources,
            )
        )
        if not enough_sources:
            reasons.append("INSUFFICIENT_INDEPENDENT_SOURCES")
            missing.append("independent_sources")
        elif len(versions) > 1:
            reasons.append("SUPPORTED_BY_INDEPENDENT_SOURCES")

    def _authority_checks(
        self,
        anchors: list[EvidenceBlock],
        requirement: EvidenceRequirement,
        signals: list[EvaluatedSignal],
        reasons: list[ReasonCode],
        missing: list[str],
    ) -> None:
        non_assessment = [b for b in anchors if not self._assessment(b)]
        satisfied = bool(non_assessment) or not requirement.require_non_assessment_source
        signals.append(
            EvaluatedSignal(
                name="non_assessment_sources",
                value=len(non_assessment),
                required=1 if requirement.require_non_assessment_source else 0,
                satisfied=satisfied,
            )
        )
        signals.append(
            EvaluatedSignal(
                name="authority_levels",
                value=sorted({b.authority_level for b in anchors}),
                satisfied=True,
            )
        )
        if not satisfied:
            reasons.append("ASSESSMENT_ONLY_EVIDENCE")
            missing.append("non_assessment_source")
        elif non_assessment:
            reasons.append("SUPPORTED_BY_NON_ASSESSMENT_SOURCE")

    def _artifact_checks(
        self,
        anchors: list[EvidenceBlock],
        requirement: EvidenceRequirement,
        signals: list[EvaluatedSignal],
        reasons: list[ReasonCode],
        missing: list[str],
    ) -> None:
        if requirement.require_table_structure:
            # M6 measures a real table context gap. A table part without its header rows cannot be
            # read correctly, and the generator must not reconstruct the missing structure.
            tables = [a for b in anchors for a in b.artifacts if a.kind == "TABLE"]
            complete = bool(tables) and all(a.header_rows for a in tables)
            signals.append(
                EvaluatedSignal(
                    name="table_structure",
                    value={
                        "tables": len(tables),
                        "with_headers": sum(bool(a.header_rows) for a in tables),
                    },
                    required="every table part carries its header rows",
                    satisfied=complete,
                )
            )
            if not complete:
                reasons.append("TABLE_STRUCTURE_INCOMPLETE")
                missing.append("table_structure")
            else:
                reasons.append("REQUIRED_ARTIFACT_PRESENT")

        if requirement.require_formula_source:
            formulas = [a for b in anchors for a in b.artifacts if a.kind == "FORMULA"]
            signals.append(
                EvaluatedSignal(
                    name="formula_source",
                    value=len(formulas),
                    required=1,
                    satisfied=bool(formulas),
                )
            )
            if not formulas:
                reasons.append("FORMULA_SOURCE_MISSING")
                missing.append("formula_source")
            else:
                reasons.append("REQUIRED_ARTIFACT_PRESENT")

        if requirement.require_visual_interpretation:
            # There is no approved vision path in M7. A caption or the structural label M3 writes
            # for a captionless figure describes that a figure exists; neither is the figure read.
            signals.append(
                EvaluatedSignal(
                    name="visual_interpretation",
                    value={
                        "required": True,
                        "vision_analysis_available": bool(self.config.vision_analysis_available),
                    },
                    required="an approved vision-analysis path",
                    satisfied=bool(self.config.vision_analysis_available),
                )
            )
            reasons.append("VISUAL_INTERPRETATION_UNAVAILABLE")
            missing.append("visual_interpretation")

    def _completeness_checks(
        self,
        evidence: EvidenceSet,
        blocks: list[EvidenceBlock],
        signals: list[EvaluatedSignal],
        reasons: list[ReasonCode],
        missing: list[str],
    ) -> None:
        omissions = [w for w in evidence.warnings if w.startswith(BUDGET_WARNING)]
        signals.append(
            EvaluatedSignal(
                name="budget_omissions",
                value=len(omissions),
                required=0,
                satisfied=not omissions,
            )
        )
        if omissions and self.config.budget_omission_is_insufficient:
            reasons.append("EVIDENCE_BUDGET_OMISSION")
            missing.append("omitted_evidence")

        partial = [b for b in blocks if b.representation == "source-spans-v1"]
        signals.append(
            EvaluatedSignal(
                name="incomplete_context_blocks",
                value=len(partial),
                required=0,
                satisfied=not partial,
            )
        )
        if partial and self.config.incomplete_context_is_insufficient:
            reasons.append("CONTEXT_INCOMPLETE")
            missing.append("complete_source_records")

        other = [w for w in evidence.warnings if not w.startswith(BUDGET_WARNING)]
        signals.append(
            EvaluatedSignal(
                name="retrieval_warnings", value=other, required=[], satisfied=not other
            )
        )
        if other:
            reasons.append("RETRIEVAL_WARNING_PRESENT")
            missing.append("clean_retrieval")

    def _decide(
        self,
        status: str,
        kind: str,
        reasons: list[ReasonCode],
        supporting: list[UUID],
        conflicting: list[UUID],
        found: list[EvidenceConflict],
        missing: list[str],
        signals: list[EvaluatedSignal],
    ) -> SufficiencyDecision:
        return SufficiencyDecision.model_validate(
            {
                "status": status,
                "question_kind": kind,
                "reason_codes": list(dict.fromkeys(reasons)),
                "evaluated_signals": signals,
                "supporting_evidence_ids": supporting,
                "conflicting_evidence_ids": conflicting,
                "conflicts": found,
                "missing_requirements": list(dict.fromkeys(missing)),
                "policy_version": self.config.version,
                "policy_fingerprint": self.config.fingerprint,
            }
        )
