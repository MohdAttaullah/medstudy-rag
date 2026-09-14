"""The held-out end-to-end layer: gate, generation contract, verification, Ask outcome.

Every decision here is made by the production objects — `SufficiencyGate`, `extract`,
`verify_claims`, `find_contradictions`, `decide` — over EvidenceSets built by the same helper the
M7 evaluation uses. What is substituted is the *provider*, and only the provider: the generator's
output is prescribed by the dataset rather than sampled from a model, and the verifier is a
deterministic double. That keeps the default evaluation free of paid API calls while leaving the
logic under test untouched, and it is why a failure here is a failure of this repository's code
rather than of a model's mood on the day.

The final mapping from pipeline state to Ask outcome is deliberately a copy of
`AskService._outcome`, and `test_m11_units.py` asserts the two agree on every reachable state. A
divergence between what the evaluation calls a refusal and what the product calls a refusal would
make every end-to-end number here quietly wrong.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.generation_config import SufficiencyConfig
from app.core.verification_config import (
    ClaimExtractionConfig,
    ClaimVerificationConfig,
    ContradictionConfig,
    FinalVerificationConfig,
    RepairConfig,
)
from app.evaluation.sufficiency import build
from app.evaluation.taxonomy import Layer, attribute, unknown_codes
from app.evaluation.verification import verifier_for
from app.generation.grounding.model import DraftClaim, GroundedDraft, ProviderSpec
from app.sufficiency.gate import SufficiencyGate
from app.verification.claims import extract
from app.verification.engine import decide, find_contradictions, verify_claims
from app.verification.model import ClaimVerification, Outcome, ReasonCode, VerificationError

GOLD = "backend/tests/fixtures/evaluation/heldout.json"
INVENTED = "__INVENTED__"

#: Outcomes the public Ask contract can return. Kept as data so the report cannot invent one.
#: `OUT_OF_SCOPE` is refused on intent before retrieval, so this offline harness — which replays
#: gate state over gold fixtures and never classifies intent — always reports zero of them. It is
#: listed anyway so the distribution enumerates the real contract rather than a subset of it.
OUTCOMES = (
    "VERIFIED",
    "INSUFFICIENT_EVIDENCE",
    "CONFLICTING_EVIDENCE",
    "UNVERIFIED",
    "FAILED",
    "OUT_OF_SCOPE",
)


def _id(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "m7:" + ":".join(parts))


def ask_outcome(*, verified: bool, sufficiency_status: str | None, provider_failed: bool) -> str:
    """The same reading of pipeline state that `AskService._outcome` performs.

    A technical failure is returned as FAILED and never as a statement about the evidence, which
    is the distinction an outage would otherwise erase.
    """
    if provider_failed:
        return "FAILED"
    if verified:
        return "VERIFIED"
    if sufficiency_status == "CONFLICTING":
        return "CONFLICTING_EVIDENCE"
    if sufficiency_status != "SUFFICIENT":
        return "INSUFFICIENT_EVIDENCE"
    return "UNVERIFIED"


def build_draft(case: dict[str, Any]) -> GroundedDraft:
    """The draft the dataset prescribes, in the real `GroundedDraft` shape.

    An `__INVENTED__` citation resolves to an evidence id that was never supplied, which is how a
    fabricated citation is exercised without asking a model to fabricate one on demand.
    """
    spec = case["draft"]
    claims = [
        DraftClaim(
            text=claim["text"],
            evidence_ids=[
                _id("evidence", "never-supplied")
                if label == INVENTED
                else _id("evidence", case["id"], label)
                for label in claim["cites"]
            ],
        )
        for claim in spec["claims"]
    ]
    return GroundedDraft(
        draft_id=_id("draft", case["id"]),
        answer=spec["answer"],
        claims=claims,
        cited_evidence_ids=[],
        uncited_evidence_ids=[],
        evidence_gap=None,
        query_hash="0" * 64,
        provider=ProviderSpec(
            provider="fake",
            model_id="evaluation-generator",
            endpoint="memory://fake",
            temperature=None,
            max_output_tokens=1024,
            schema_version="grounded-draft-schema-v1",
            prompt_version="grounded-draft-v1",
        ),
        grounding_policy_version="grounding-m7-v1",
        grounding_policy_fingerprint="eval",
        sufficiency_policy_fingerprint="eval",
        durations_ms={},
    )


def _verify(case: dict[str, Any], evidence: list[Any]) -> tuple[Outcome, list[str], int, int, int]:
    """Run the real claim-verification loop. Returns outcome, codes, repairs, material, failed."""
    by_id = {str(block.evidence_id): block for block in evidence}
    draft = build_draft(case)
    verifier = verifier_for(case.get("verifier", "SUPPORTED"))
    repair = RepairConfig(enabled=case.get("repair_enabled", True))
    repair_count = 0
    outcome: Outcome = "ABSTAIN"
    codes: list[ReasonCode] = []
    verifications: list[ClaimVerification] = []

    while True:
        claims = extract(draft, by_id, ClaimExtractionConfig())
        try:
            verifications = asyncio.run(
                verify_claims(claims, evidence, _id("tenant"), verifier, ClaimVerificationConfig())
            )
        except VerificationError as exc:
            # Fail closed: a verifier that could not run has approved nothing.
            return "ABSTAIN", [exc.code], repair_count, 0, 0
        contradictions = find_contradictions(claims, evidence, ContradictionConfig())
        outcome, codes = decide(
            verifications, contradictions, repair_count, repair.enabled, FinalVerificationConfig()
        )
        if outcome != "REGENERATE_ONCE":
            break
        repair_count += 1

    material = [v for v in verifications if v.material]
    return (
        outcome,
        [str(code) for code in codes],
        repair_count,
        len(material),
        sum(v.failed for v in material),
    )


def run_case(
    case: dict[str, Any], corpus: dict[str, Any], config: SufficiencyConfig
) -> dict[str, Any]:
    evidence_set = build(case, corpus)
    decision = SufficiencyGate(config).evaluate(case["question"], evidence_set)
    status = decision.status
    codes = [str(code) for code in decision.reason_codes]

    provider_failed = False
    verified = False
    verification_outcome: str | None = None
    repair_count = material = unsupported = 0

    if status == "SUFFICIENT":
        if case.get("provider_failure"):
            # The gate allowed a draft and the provider never produced one.
            provider_failed = True
            codes = [case["provider_failure"]]
        elif "draft" in case:
            result, verify_codes, repair_count, material, unsupported = _verify(
                case, list(evidence_set.evidence_blocks)
            )
            verification_outcome = result
            verified = result == "PASS"
            codes = verify_codes if not verified else []
        else:
            # A SUFFICIENT case with neither a draft nor a declared outage is a dataset error, not
            # a system result. Surfacing it as a case failure keeps it from scoring as an answer.
            codes = ["DATASET_CASE_INCOMPLETE"]

    outcome = ask_outcome(
        verified=verified, sufficiency_status=status, provider_failed=provider_failed
    )
    expected_outcome = case["expected_outcome"]
    expected_codes = case.get("expected_reason_codes", [])
    attributed = attribute(codes)

    return {
        "case": case["id"],
        "category": case["category"],
        "question": case["question"],
        "answerable": case["answerable"],
        "expected_gate": case["expected_gate"],
        "actual_gate": status,
        "gate_agreed": status == case["expected_gate"],
        "expected_outcome": expected_outcome,
        "actual_outcome": outcome,
        "outcome_agreed": outcome == expected_outcome,
        "verified": verified,
        "answer": build_draft(case).answer if verified else None,
        "verification_outcome": verification_outcome,
        "repair_count": repair_count,
        "expected_repair": bool(case.get("expected_repair")),
        "material_claims": material,
        "unsupported_claims": unsupported,
        "reason_codes": codes,
        "expected_reason_codes": expected_codes,
        # Every declared expectation must appear, not merely one of them: a case that expected
        # a numeric mismatch and got an unrelated refusal has not shown what it was written for.
        "reason_codes_matched": all(code in codes for code in expected_codes),
        "unknown_reason_codes": list(unknown_codes(codes)),
        "attributed_layer": attributed,
        "expected_layer": case.get("expected_layer"),
        "layer_agreed": (
            attributed == case["expected_layer"] if case.get("expected_layer") else None
        ),
        "note": case.get("note"),
    }


def evaluate(root: Path, config: SufficiencyConfig | None = None) -> dict[str, Any]:
    gold = json.loads((root / GOLD).read_text(encoding="utf-8"))
    policy = config or SufficiencyConfig()
    corpus = {"documents": gold["documents"], "chunks": gold["chunks"]}
    cases = [run_case(case, corpus, policy) for case in gold["cases"]]

    # The two failures that are not interchangeable with anything else in this report.
    answered_when_unanswerable = [
        c for c in cases if c["actual_outcome"] == "VERIFIED" and not c["answerable"]
    ]
    answer_without_verified = [
        c for c in cases if c["answer"] is not None and c["actual_outcome"] != "VERIFIED"
    ]
    refused_when_answerable = [
        c
        for c in cases
        if c["answerable"]
        and c["expected_outcome"] == "VERIFIED"
        and c["actual_outcome"] != "VERIFIED"
    ]
    attributed = [c for c in cases if c["expected_layer"]]

    return {
        "dataset_id": gold["dataset_id"],
        "dataset_version": gold["dataset_version"],
        "notice": gold["notice"],
        "policy_frozen_at": gold["policy_frozen_at"],
        "independence": "HELD_OUT",
        "sufficiency_policy_fingerprint": policy.fingerprint,
        "cases_evaluated": len(cases),
        "gate_agreement": sum(c["gate_agreed"] for c in cases) / len(cases),
        "outcome_agreement": sum(c["outcome_agreed"] for c in cases) / len(cases),
        "outcome_distribution": {
            outcome: sum(c["actual_outcome"] == outcome for c in cases) for outcome in OUTCOMES
        },
        "abstention_rate": sum(c["actual_outcome"] != "VERIFIED" for c in cases) / len(cases),
        "answered_when_unanswerable_count": len(answered_when_unanswerable),
        "answered_when_unanswerable": [c["case"] for c in answered_when_unanswerable],
        "answer_without_verified_count": len(answer_without_verified),
        "refused_when_answerable_count": len(refused_when_answerable),
        "refused_when_answerable": [c["case"] for c in refused_when_answerable],
        "reason_codes_matched": sum(
            c["reason_codes_matched"] for c in cases if c["expected_reason_codes"]
        ),
        "reason_code_cases": sum(1 for c in cases if c["expected_reason_codes"]),
        "layer_attribution_agreement": (
            sum(bool(c["layer_agreed"]) for c in attributed) / len(attributed)
            if attributed
            else None
        ),
        "layer_attribution_cases": len(attributed),
        "unknown_reason_codes": sorted({code for c in cases for code in c["unknown_reason_codes"]}),
        "failure_layers": _layer_counts(cases),
        "repair_attempted": sum(bool(c["repair_count"]) for c in cases),
        "repair_cap_respected": all(c["repair_count"] <= 1 for c in cases),
        "clinically_validated": False,
        "cases": cases,
    }


def _layer_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        layer = cast(Layer | None, case["attributed_layer"])
        if layer and case["actual_outcome"] != "VERIFIED":
            counts[layer] = counts.get(layer, 0) + 1
    return dict(sorted(counts.items()))
