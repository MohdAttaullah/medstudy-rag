"""One controlled live provider smoke for the M7 draft path.

Run inside the API container, where the backend-only provider key lives. It exercises the real
sufficiency gate, the real provider adapter, the real citation binding and the real response
contract; only M5 retrieval and M6 reranking are stubbed, because they are already verified and
their live behaviour is not what a provider smoke is for.

The evidence is deliberately synthetic, non-sensitive and written for this file. The corpus-driven
smokes abstain at the gate on every parsing fixture, so they never reach a provider — which is
correct behaviour and exactly why a separate, gate-passing case is needed to verify the integration.

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
from app.schemas.generation import DraftResponse
from app.security.auth import Principal
from app.services.generation import GenerationService

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
    parser.add_argument("--out", type=Path, default=Path("/tmp/m7-live-smoke.json"))
    args = parser.parse_args()

    settings = Settings()
    selection = settings.generator
    assert selection is not None, "No generator is configured; nothing to smoke."
    assert selection.model_id and not selection.model_id.startswith("<"), (
        f"MEDRAG_GENERATOR__MODEL_ID is a placeholder: {selection.model_id!r}"
    )
    service = GenerationService(StubEvidenceService(NoMetrics()), settings)
    actor = Principal(user_id=uuid4(), tenant_id=uuid4(), display_name="Live smoke", role="admin")

    result = await service.draft(actor, QUESTION, uuid4())

    # The whole API contract must still validate, not just the draft.
    validated = DraftResponse.model_validate(result)
    assert validated.answering_enabled is False
    assert validated.verified is False

    decision = validated.sufficiency
    assert decision.status == "SUFFICIENT", (decision.status, decision.reason_codes)
    assert validated.abstention is None

    draft = validated.draft
    assert draft is not None, "The gate permitted generation but no draft was produced."
    assert draft.status == "GROUNDED_DRAFT"
    assert draft.verification_status == "UNVERIFIED_AWAITING_CLAIM_VERIFICATION"

    supplied = {b.evidence_id for b in validated.evidence_set.evidence_blocks}
    assert draft.claims, "A draft must bind at least one statement to evidence."
    for claim in draft.claims:
        assert claim.evidence_ids, "Every claim must cite evidence."
        assert set(claim.evidence_ids) <= supplied, "A citation named unsupplied evidence."
    assert set(draft.cited_evidence_ids) <= supplied

    assert draft.provider.provider == selection.provider
    assert draft.provider.model_id == selection.model_id
    assert draft.provider.prompt_version == "grounded-draft-v1"
    assert draft.provider.schema_version == "grounded-draft-schema-v1"
    assert draft.grounding_policy_fingerprint == settings.grounding.fingerprint
    assert draft.sufficiency_policy_fingerprint == decision.policy_fingerprint

    body = validated.model_dump_json()
    for forbidden in ('"api_key"', "sk-", '"verified":true', '"answering_enabled":true'):
        assert forbidden not in body.replace(" ", ""), forbidden

    record = {
        "provider": draft.provider.provider,
        "model": draft.provider.model_id,
        "request_succeeded": True,
        "sufficiency_status": decision.status,
        "sufficiency_reason_codes": list(decision.reason_codes),
        "schema_valid": True,
        "citations_valid": True,
        "claims": len(draft.claims),
        "cited_evidence_ids": [str(v) for v in draft.cited_evidence_ids],
        "uncited_evidence_ids": [str(v) for v in draft.uncited_evidence_ids],
        "evidence_blocks_supplied": len(supplied),
        "verification_status": draft.verification_status,
        "verified": validated.verified,
        "answering_enabled": validated.answering_enabled,
        "answer_characters": len(draft.answer),
        "evidence_gap_reported": draft.evidence_gap is not None,
        "provider_latency_ms": round(draft.durations_ms["provider_ms"], 1),
        "pipeline_total_ms": round(validated.durations_ms["pipeline_total_ms"], 1),
        "sufficiency_ms": round(validated.durations_ms["sufficiency_ms"], 3),
    }
    args.out.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print("PASS: live provider produced a schema-valid, citation-bound, UNVERIFIED grounded draft.")
    print(json.dumps(record, indent=2))
    # The draft text itself is model output over synthetic evidence; printed last and only so a
    # reviewer can see it is grounded prose rather than a refusal.
    print("\n--- draft answer (synthetic evidence) ---\n" + draft.answer)


if __name__ == "__main__":
    asyncio.run(main())
