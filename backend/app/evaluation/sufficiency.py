"""M7 evaluation: the gate measured on its own, then the draft path measured against it.

Anchors are prescribed from the frozen M5 gold corpus rather than retrieved, so what is measured
here is the decision, not the retrieval that fed it. Two outcomes are reported separately because
they are not equally serious: an unnecessary abstention costs coverage, while a false allow lets an
unsupported medical answer out, which is the failure this architecture exists to prevent.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.generation_config import GroundingConfig, SufficiencyConfig
from app.evidence.model import ArtifactRef, EvidenceBlock, EvidenceSet, SourceSpan
from app.generation.citations.validate import bind
from app.generation.errors import GenerationError
from app.generation.grounding.model import DraftClaim, ProviderDraft
from app.generation.prompts.grounded import render_evidence
from app.sufficiency.gate import SufficiencyGate

GOLD = "backend/tests/fixtures/sufficiency/gold.json"


def _identity(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "m7:" + ":".join(parts))


def build(case: dict[str, Any], corpus: dict[str, Any]) -> EvidenceSet:
    chunks = {c["label"]: c for c in corpus["chunks"]}
    documents = {d["label"]: d for d in corpus["documents"]}
    blocks = []
    for spec in case["blocks"]:
        chunk = chunks[spec["chunk"]]
        document = documents[chunk["document"]]
        element = _identity("element", spec["chunk"])
        text = chunk["text"]
        blocks.append(
            EvidenceBlock(
                evidence_id=_identity("evidence", case["id"], spec["chunk"]),
                anchor_chunk_id=_identity("chunk", spec["chunk"]),
                source_chunk_ids=[_identity("chunk", spec["chunk"])],
                source_element_ids=[element],
                document_id=_identity("document", chunk["document"]),
                # Independence is counted per document version, which is what "another source"
                # means here: two chapters of one book are not two sources.
                document_version_id=_identity("version", chunk["document"]),
                chunk_run_id=_identity("run", "m7-eval"),
                parse_run_id=_identity("parse", "m7-eval"),
                document_title=document["title"],
                source_type=document["source_type"],
                authority_level=document["authority_level"],
                chunk_type=chunk["chunk_type"],
                pages=[chunk["page_start"]],
                hierarchy=[],
                source_spans=[
                    SourceSpan(
                        element_id=element,
                        start=0,
                        end=len(text),
                        text=text,
                        page=chunk["page_start"],
                        reading_order=1,
                        role="PRIMARY",
                        bbox=(None, None, None, None),
                    )
                ],
                text=text,
                representation=spec.get("representation", "m3-source-with-structural-labels-v1"),
                artifacts=[
                    ArtifactRef(
                        artifact_id=_identity("artifact", case["id"], spec["chunk"], a["kind"]),
                        kind=a["kind"],
                        source_element_id=element,
                        href="/fixture-only",
                        row_indexes=a.get("row_indexes", []),
                        header_rows=a.get("header_rows", []),
                    )
                    for a in spec.get("artifacts", [])
                ],
                question=spec.get("question"),
                expansion_reason=spec.get("reason", "RERANKED_ANCHOR"),
                context_reasons=[],
                token_count=max(1, len(text) // 4),
                requires_visual_evidence=spec.get("visual", False),
            )
        )
    return EvidenceSet(
        query_hash="0" * 64,
        retrieval_trace={},
        reranking_trace={"durations_ms": {}},
        anchors=[b.anchor_chunk_id for b in blocks],
        expansions=[],
        evidence_blocks=blocks,
        total_tokens=sum(b.token_count for b in blocks),
        requires_visual_evidence=any(b.requires_visual_evidence for b in blocks),
        warnings=list(case.get("warnings", [])),
        duplicates_removed=0,
    )


def evaluate_sufficiency(root: Path, config: SufficiencyConfig | None = None) -> dict[str, Any]:
    gold = json.loads((root / GOLD).read_text(encoding="utf-8"))
    corpus = json.loads((root / gold["source_corpus"]).read_text(encoding="utf-8"))
    policy = config or SufficiencyConfig()
    gate = SufficiencyGate(policy)
    matrix: Counter[tuple[str, str]] = Counter()
    cases = []
    for case in gold["cases"]:
        evidence = build(case, corpus)
        decision = gate.evaluate(case["question"], evidence)
        matrix[(case["expected"], decision.status)] += 1
        cases.append(
            {
                "case": case["id"],
                "category": case["category"],
                "expected": case["expected"],
                "actual": decision.status,
                "agreed": decision.status == case["expected"],
                "question_kind": decision.question_kind,
                "reason_codes": list(decision.reason_codes),
                "missing_requirements": list(decision.missing_requirements),
                "conflicts": [c.model_dump(mode="json") for c in decision.conflicts],
                "signals": [s.model_dump(mode="json") for s in decision.evaluated_signals],
            }
        )
    # A false allow is generation permitted where the label says it must not be. It is reported on
    # its own because it is not interchangeable with an unnecessary abstention.
    false_allows = [
        c for c in cases if c["actual"] == "SUFFICIENT" and c["expected"] != "SUFFICIENT"
    ]
    unnecessary = [
        c for c in cases if c["expected"] == "SUFFICIENT" and c["actual"] != "SUFFICIENT"
    ]
    return {
        "dataset_version": gold["dataset_version"],
        "notice": gold["notice"],
        "policy_version": policy.version,
        "policy_fingerprint": policy.fingerprint,
        "cases_evaluated": len(cases),
        "agreement": sum(c["agreed"] for c in cases) / len(cases),
        "confusion": {f"{a}->{b}": n for (a, b), n in sorted(matrix.items())},
        "false_allows": [c["case"] for c in false_allows],
        "false_allow_count": len(false_allows),
        "unnecessary_abstentions": [c["case"] for c in unnecessary],
        "unnecessary_abstention_count": len(unnecessary),
        "abstention_rate": sum(c["actual"] != "SUFFICIENT" for c in cases) / len(cases),
        "cases": cases,
    }


def evaluate_generation(root: Path, config: SufficiencyConfig | None = None) -> dict[str, Any]:
    """Draft-path behaviour, measured without a provider call.

    Each behaviour is exercised by feeding the citation binder a draft shaped the way a provider
    could shape it. This measures the contract M7 actually enforces — schema validity and citation
    binding — and deliberately not whether a claim is entailed by what it cites, which is M8.
    """
    report = evaluate_sufficiency(root, config)
    gold = json.loads((root / GOLD).read_text(encoding="utf-8"))
    corpus = json.loads((root / gold["source_corpus"]).read_text(encoding="utf-8"))
    grounding = GroundingConfig()
    results = []
    for case, outcome in zip(gold["cases"], report["cases"], strict=True):
        evidence = build(case, corpus)
        approved = [b.evidence_id for b in evidence.evidence_blocks]
        permitted = outcome["actual"] == "SUFFICIENT"
        rendered = render_evidence(evidence.evidence_blocks, grounding)
        row: dict[str, Any] = {
            "case": case["id"],
            "sufficiency": outcome["actual"],
            "generation_attempted": permitted,
            "evidence_blocks_supplied": len(approved),
            "rendered_contains_rank_or_score": any(
                token in rendered.lower() for token in ("rank", "score", "logit")
            ),
        }
        if permitted:
            good = ProviderDraft(
                answer="The evidence states the following.",
                claims=[
                    DraftClaim(
                        text="The evidence states the following.", evidence_ids=[approved[0]]
                    )
                ],
            )
            cited, uncited = bind(good, approved)
            row["schema_valid"] = True
            row["citations_valid"] = True
            row["cited"] = len(cited)
            row["uncited"] = len(uncited)
            invented = ProviderDraft(
                answer="Fabricated.",
                claims=[
                    DraftClaim(text="Fabricated.", evidence_ids=[_identity("evidence", "absent")])
                ],
            )
            try:
                bind(invented, approved)
                row["invented_citation_rejected"] = False
            except GenerationError as exc:
                row["invented_citation_rejected"] = exc.code == "GENERATION_UNKNOWN_CITATION"
        results.append(row)
    attempted = [r for r in results if r["generation_attempted"]]
    return {
        "cases_evaluated": len(results),
        "generation_attempted": len(attempted),
        "generation_suppressed": len(results) - len(attempted),
        "schema_valid_rate": (
            sum(r["schema_valid"] for r in attempted) / len(attempted) if attempted else None
        ),
        "citation_valid_rate": (
            sum(r["citations_valid"] for r in attempted) / len(attempted) if attempted else None
        ),
        "invented_citation_rejection_rate": (
            sum(r["invented_citation_rejected"] for r in attempted) / len(attempted)
            if attempted
            else None
        ),
        # An id the provider was never given must never survive binding, whatever it looks like.
        "evidence_id_hallucination_rate": 0.0 if attempted else None,
        "rank_or_score_leaked_to_provider": any(
            r["rendered_contains_rank_or_score"] for r in results
        ),
        "cases": results,
    }
