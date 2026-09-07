"""M8 evaluation: what verification catches, and whether it ever releases what it must not.

The metric that matters is the false PASS — an unsupported or contradicted material claim released
as a verified answer. It is reported on its own, never averaged into an accuracy figure, because it
is not interchangeable with an unnecessary abstention: one emits a wrong medical answer and the
other emits nothing.
"""

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.verification_config import (
    ClaimExtractionConfig,
    ClaimVerificationConfig,
    ContradictionConfig,
    FinalVerificationConfig,
    RepairConfig,
)
from app.evidence.model import ArtifactRef, EvidenceBlock, SourceSpan
from app.generation.grounding.model import DraftClaim, GroundedDraft, ProviderSpec
from app.verification.claims import extract
from app.verification.engine import decide, find_contradictions, verify_claims
from app.verification.model import (
    ClaimVerification,
    ContradictionFinding,
    Outcome,
    ReasonCode,
    Verdict,
    VerificationError,
)
from app.verification.verifier import FakeClaimVerifier, VerifierVerdict

GOLD = "backend/tests/fixtures/verification/gold.json"
INVENTED = "__invented__"


def _id(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "m8:" + ":".join(parts))


def build_block(spec: dict[str, Any]) -> EvidenceBlock:
    element = _id("element", spec["label"])
    text = spec["text"]
    return EvidenceBlock(
        evidence_id=_id("evidence", spec["label"]),
        anchor_chunk_id=_id("chunk", spec["label"]),
        source_chunk_ids=[_id("chunk", spec["label"])],
        source_element_ids=[element],
        document_id=_id("document", spec["label"]),
        document_version_id=_id("version", spec["title"], spec["source_type"]),
        chunk_run_id=_id("run", "m8-eval"),
        parse_run_id=_id("parse", "m8-eval"),
        document_title=spec["title"],
        source_type=spec["source_type"],
        authority_level=spec["authority_level"],
        chunk_type=spec.get("chunk_type", "TEXT_CHILD"),
        pages=[1],
        hierarchy=[],
        source_spans=[
            SourceSpan(
                element_id=element,
                start=0,
                end=len(text),
                text=text,
                page=1,
                reading_order=1,
                role="PRIMARY",
                bbox=(None, None, None, None),
            )
        ],
        text=text,
        representation="m3-source-with-structural-labels-v1",
        artifacts=[
            ArtifactRef(
                artifact_id=_id("artifact", spec["label"], a["kind"]),
                kind=a["kind"],
                source_element_id=element,
                href="/fixture-only",
                row_indexes=a.get("row_indexes", []),
                header_rows=a.get("header_rows", []),
            )
            for a in spec.get("artifacts", [])
        ],
        question=None,
        expansion_reason="RERANKED_ANCHOR",
        context_reasons=[],
        token_count=max(1, len(text) // 4),
        requires_visual_evidence=spec.get("visual", False),
    )


def build_draft(case: dict[str, Any]) -> GroundedDraft:
    claims = []
    for claim in case["claims"]:
        ids = [
            _id("evidence", "absent-block") if label == INVENTED else _id("evidence", label)
            for label in claim["cites"]
        ]
        claims.append(DraftClaim(text=claim["text"], evidence_ids=ids))
    return GroundedDraft(
        draft_id=_id("draft", case["id"]),
        answer=case["answer"],
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


def verifier_for(mode: str) -> FakeClaimVerifier:
    """One verifier behaviour per fixture, including the two failure modes."""
    if mode == "FAILED":
        return FakeClaimVerifier(lambda claim: VerificationError("VERIFIER_FAILED"))
    if mode == "MALFORMED":
        return FakeClaimVerifier(lambda claim: {"verdict": "NOT_A_VALID_VERDICT"})
    if mode == "FAIL_THEN_SUPPORT":
        state: list[int] = []

        def decide_once(claim: Any) -> VerifierVerdict:
            state.append(1)
            return VerifierVerdict(verdict="SUPPORTED" if len(state) > 1 else "UNSUPPORTED")

        return FakeClaimVerifier(decide_once)
    return FakeClaimVerifier(lambda claim: VerifierVerdict(verdict=cast(Verdict, mode)))


def run_case(case: dict[str, Any], blocks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    evidence = [build_block(blocks[label]) for label in case["blocks"]]
    by_id = {str(b.evidence_id): b for b in evidence}
    draft = build_draft(case)
    verifier = verifier_for(case["verifier"])
    repair = RepairConfig(enabled=case.get("repair_enabled", True))
    repair_count = 0
    outcome: Outcome = "ABSTAIN"
    codes: list[ReasonCode] = []
    verifications: list[ClaimVerification] = []
    contradictions: list[ContradictionFinding] = []

    while True:
        claims = extract(draft, by_id, ClaimExtractionConfig())
        try:
            verifications = asyncio.run(
                verify_claims(claims, evidence, _id("tenant"), verifier, ClaimVerificationConfig())
            )
        except VerificationError as exc:
            # Fail closed: a verifier that could not run has approved nothing.
            outcome, codes = "ABSTAIN", [cast(ReasonCode, exc.code)]
            break
        contradictions = find_contradictions(claims, evidence, ContradictionConfig())
        outcome, codes = decide(
            verifications, contradictions, repair_count, repair.enabled, FinalVerificationConfig()
        )
        if outcome != "REGENERATE_ONCE":
            break
        # The repaired draft is identical here; only the verifier's second opinion differs. The
        # point being measured is that a repair happens once and is then re-verified in full.
        repair_count += 1

    material = [v for v in verifications if v.material]
    return {
        "case": case["id"],
        "category": case["category"],
        "expected": case["expected"],
        "actual": outcome,
        "agreed": outcome == case["expected"],
        "repair_count": repair_count,
        "expected_repair": case.get("expect_repair", 0),
        "material_claims": len(material),
        "supported_claims": sum(v.verdict == "SUPPORTED" for v in material),
        "unsupported_claims": sum(v.failed for v in material),
        "reason_codes": [str(c) for c in codes],
        "expected_codes": case.get("expect_codes", []),
        "codes_matched": all(c in [str(x) for x in codes] for c in case.get("expect_codes", []))
        or any(c in [str(x) for x in codes] for c in case.get("expect_codes", [])),
        "contradictions": [f.kind for f in contradictions],
    }


def evaluate(root: Path) -> dict[str, Any]:
    gold = json.loads((root / GOLD).read_text(encoding="utf-8"))
    blocks = {b["label"]: b for b in gold["blocks"]}
    results = [run_case(case, blocks) for case in gold["cases"]]

    # A false PASS is an unsupported or contradicted answer released as verified. Nothing else in
    # this report is comparable to it.
    false_passes = [r for r in results if r["actual"] == "PASS" and r["expected"] != "PASS"]
    missed = [r for r in results if r["expected"] == "PASS" and r["actual"] != "PASS"]
    detection = [r for r in results if r["expected"] == "ABSTAIN"]
    repairs = [r for r in results if r["expected_repair"]]
    matrix: Counter[str] = Counter(f"{r['expected']}->{r['actual']}" for r in results)

    return {
        "dataset_version": gold["dataset_version"],
        "notice": gold["notice"],
        "cases_evaluated": len(results),
        "confusion": dict(sorted(matrix.items())),
        "agreement": sum(r["agreed"] for r in results) / len(results),
        "false_pass_count": len(false_passes),
        "false_pass_rate": len(false_passes) / len(results),
        "false_passes": [r["case"] for r in false_passes],
        "unnecessary_abstentions": [r["case"] for r in missed],
        "unsupported_claim_detection_rate": (
            sum(r["actual"] == "ABSTAIN" for r in detection) / len(detection) if detection else None
        ),
        "reason_code_precision": (
            sum(r["codes_matched"] for r in results if r["expected_codes"])
            / max(1, sum(1 for r in results if r["expected_codes"]))
        ),
        "citation_failures_detected": sum(
            "UNKNOWN_CITATION" in r["reason_codes"] or "PROVENANCE_UNRESOLVED" in r["reason_codes"]
            for r in results
        ),
        "contradiction_cases_detected": sum(bool(r["contradictions"]) for r in results),
        "repair_attempted": sum(bool(r["repair_count"]) for r in results),
        "repair_success_rate": (
            sum(r["repair_count"] and r["actual"] == "PASS" for r in repairs) / len(repairs)
            if repairs
            else None
        ),
        "repair_cap_respected": all(r["repair_count"] <= 1 for r in results),
        "post_repair_unsupported": sum(
            r["repair_count"] and r["unsupported_claims"] > 0 for r in results
        ),
        "abstention_rate": sum(r["actual"] != "PASS" for r in results) / len(results),
        "cases": results,
    }
