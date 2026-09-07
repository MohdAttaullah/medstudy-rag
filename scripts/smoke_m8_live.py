"""One controlled live smoke for the full M7 + M8 path.

Run inside the API container, where the backend-only provider key lives. It exercises the real
sufficiency gate, the real generator adapter, real claim extraction, the real deterministic checks,
the real semantic verifier and the real response contract; only M5 retrieval and M6 reranking are
stubbed, because a provider smoke is not the place to re-verify them.

The evidence is deliberately synthetic, non-sensitive and written for this file. The corpus-driven
smokes abstain at the gate on every parsing fixture, so they never reach a provider at all — which
is correct behaviour and exactly why a separate, gate-passing case is needed.

Prints no key, no prompt and no provider error body.
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.core.config import Settings
from app.evidence.model import EvidenceSet
from app.schemas.verification import AnswerResponse
from app.security.auth import Principal
from app.services.generation import GenerationService
from app.services.verification import VerificationService

QUESTION = "What does the reference say about the Alpha parameter group and its stated unit?"

# Two independent synthetic sources. Non-sensitive, invented for this smoke, and deliberately
# free of any real clinical instruction: the point is the contract, not the content.
SOURCES = [
    (
        "Synthetic Reference Volume (Smoke Corpus)",
        "REFERENCE_BOOK",
        "REFERENCE",
        "The Alpha parameter group is recorded in illustrative units of millilitres per minute. "
        "This passage is synthetic evaluation content and describes no real measurement.",
    ),
    (
        "Synthetic Practice Guideline (Smoke Corpus)",
        "GUIDELINE",
        "HIGH",
        "The Alpha parameter group is reported in the same illustrative units of millilitres per "
        "minute. This passage is synthetic evaluation content and states no clinical guidance.",
    ),
]


def _id(*parts: str):
    return uuid5(NAMESPACE_URL, "m7-live-smoke:" + ":".join(parts))


def evidence_set() -> EvidenceSet:
    blocks = []
    for index, (title, source_type, authority, text) in enumerate(SOURCES):
        element = _id("element", str(index))
        blocks.append(
            {
                "evidence_id": _id("evidence", str(index)),
                "anchor_chunk_id": _id("chunk", str(index)),
                "source_chunk_ids": [_id("chunk", str(index))],
                "source_element_ids": [element],
                "document_id": _id("document", str(index)),
                "document_version_id": _id("version", str(index)),
                "chunk_run_id": _id("run", "smoke"),
                "parse_run_id": _id("parse", "smoke"),
                "document_title": title,
                "source_type": source_type,
                "authority_level": authority,
                "chunk_type": "TEXT_CHILD",
                "pages": [index + 1],
                "hierarchy": [],
                "source_spans": [
                    {
                        "element_id": element,
                        "start": 0,
                        "end": len(text),
                        "text": text,
                        "page": index + 1,
                        "reading_order": 1,
                        "role": "PRIMARY",
                        "bbox": (None, None, None, None),
                    }
                ],
                "text": text,
                "representation": "m3-source-with-structural-labels-v1",
                "artifacts": [],
                "question": None,
                "expansion_reason": "RERANKED_ANCHOR",
                "context_reasons": [],
                "token_count": max(1, len(text) // 4),
                "requires_visual_evidence": False,
            }
        )
    return EvidenceSet.model_validate(
        {
            "query_hash": "0" * 64,
            "retrieval_trace": {},
            "reranking_trace": {"durations_ms": {"pipeline_total_ms": 0.0}},
            "anchors": [b["anchor_chunk_id"] for b in blocks],
            "expansions": [],
            "evidence_blocks": blocks,
            "total_tokens": sum(int(b["token_count"]) for b in blocks),
            "requires_visual_evidence": False,
            "warnings": [],
            "duplicates_removed": 0,
        }
    )


class StubEvidenceService:
    """Stands in for the verified M5+M6 stages only. The gate and everything after it are real."""

    def __init__(self, retrieval: Any) -> None:
        self.retrieval = retrieval

    def search(self, actor, query, correlation_id, filters=None) -> dict[str, Any]:
        evidence = evidence_set()
        return {
            "correlation_id": correlation_id,
            "mode": "RERANKED",
            "answering_enabled": False,
            "first_stage": {
                "correlation_id": correlation_id,
                "mode": "HYBRID_RRF",
                "candidates": [],
                "dense": [],
                "sparse": [],
                "warnings": [],
                "answering_enabled": False,
                "trace": {
                    "correlation_id": correlation_id,
                    "mode": "HYBRID_RRF",
                    "retrieval_config_version": "live-smoke",
                    "retrieval_config_fingerprint": "live-smoke",
                    "query_hash": "0" * 64,
                    "normalization_version": "live-smoke",
                    "durations_ms": {},
                    # The stub stands in for M5/M6, so its trace must still satisfy their
                    # contract: a response that skipped required diagnostics would not prove
                    # the real endpoint shape still validates.
                    "dense_top_k": 0,
                    "sparse_top_k": 0,
                    "final_top_k": 0,
                    "rrf_k": 60,
                    "dense_weight": 1.0,
                    "sparse_weight": 1.0,
                    "bm25_k1": 1.2,
                    "bm25_b": 0.75,
                    "dense_candidates": 0,
                    "sparse_candidates": 0,
                    "fused_candidates": 0,
                },
            },
            "reranked": [],
            "evidence_set": evidence.model_dump(mode="json"),
        }


class NoMetrics:
    metrics = None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/m8-live-smoke.json"))
    args = parser.parse_args()

    settings = Settings()
    selection = settings.generator
    assert selection is not None, "No generator is configured; nothing to smoke."
    assert selection.model_id and not selection.model_id.startswith("<"), (
        f"MEDRAG_GENERATOR__MODEL_ID is a placeholder: {selection.model_id!r}"
    )
    generation = GenerationService(StubEvidenceService(NoMetrics()), settings)
    service = VerificationService(generation, settings)
    actor = Principal(user_id=uuid4(), tenant_id=uuid4(), display_name="Live smoke", role="admin")

    result = await service.answer(actor, QUESTION, uuid4())

    validated = AnswerResponse.model_validate(result)
    # Releasing a verified answer is still not the user-facing Ask experience, which is M9.
    assert validated.answering_enabled is False
    assert validated.sufficiency.status == "SUFFICIENT", validated.sufficiency.reason_codes
    assert validated.draft is not None, "the gate permitted generation but no draft was produced"
    assert validated.draft.verification_status == "UNVERIFIED_AWAITING_CLAIM_VERIFICATION"

    report = validated.verification
    assert report is not None, "a draft was produced but never verified"
    assert report.repair_count <= 1, "a repair may happen at most once"
    assert report.verifier is not None
    assert report.claims, "claim extraction produced nothing to verify"

    supplied = {b.evidence_id for b in validated.evidence_set.evidence_blocks}
    for verification in report.verifications:
        for evidence_id in (
            *verification.supporting_evidence_ids,
            *verification.contradicting_evidence_ids,
        ):
            assert evidence_id in supplied, "a verdict named evidence nobody supplied"

    answer = validated.verified_answer
    # The one invariant that matters: verified is true only behind a released, PASSed answer.
    assert validated.verified is (answer is not None)
    if answer is not None:
        assert report.outcome == "PASS"
        assert answer.verified is True and answer.verification_status == "VERIFIED"
        assert answer.claims and all(c.verdict == "SUPPORTED" for c in answer.claims)
        assert set(answer.cited_evidence_ids) <= supplied
    else:
        assert report.outcome == "ABSTAIN"
        assert validated.verification_abstention is not None

    text = validated.model_dump_json()
    for forbidden in ('"api_key"', "sk-", '"answering_enabled":true', '"confidence"'):
        assert forbidden not in text.replace(" ", ""), forbidden

    record = {
        "generator": validated.draft.provider.model_dump(mode="json"),
        "verifier": report.verifier.model_dump(mode="json"),
        "request_succeeded": True,
        "sufficiency_status": validated.sufficiency.status,
        "verification_outcome": report.outcome,
        "verified": validated.verified,
        "material_claims": report.material_claims,
        "supported_claims": report.supported_claims,
        "repair_count": report.repair_count,
        "failed_reason_codes": [str(c) for c in report.failed_reason_codes],
        "contradictions": [f.kind for f in report.contradictions],
        "schema_valid": True,
        "citations_valid": True,
        "claim_verdicts": [
            {"claim": v.claim_text, "type": v.claim_type, "verdict": v.verdict}
            for v in report.verifications
            if v.material
        ],
        "durations_ms": {k: round(v, 1) for k, v in validated.durations_ms.items()},
        "verification_stage_ms": {k: round(v, 1) for k, v in report.durations_ms.items()},
    }
    args.out.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(
        "PASS: live generator and verifier ran; nothing was released as verified without every "
        "material claim being checked."
    )
    print(json.dumps(record, indent=2))
    if answer is not None:
        print("\n--- verified answer (synthetic evidence) ---\n" + answer.answer)
    else:
        print("\n--- abstained ---\n" + validated.verification_abstention.message)


if __name__ == "__main__":
    asyncio.run(main())
