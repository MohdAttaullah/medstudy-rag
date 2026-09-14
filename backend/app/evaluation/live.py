"""Opt-in live provider evaluation. Never reached unless `--live` was passed explicitly.

What this measures is the *integration*: that the configured provider returns output matching the
grounded-draft schema, that every citation it emits is an evidence id that was actually supplied,
that the verifier runs against real output, and what it costs in latency and tokens. A handful of
synthetic questions cannot establish that a model is reliable, and this module records nothing
that would support such a claim.

The API key is read by the existing adapter from settings and never appears in a return value, a
log line or an artifact. Only provider and model *identity* are recorded.
"""

import asyncio
import time
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any

from app.core.config import Settings
from app.core.errors import DomainError
from app.evaluation.end_to_end import GOLD
from app.evaluation.sufficiency import build
from app.evaluation.taxonomy import attribute
from app.generation.grounding.model import DeclinedDraft

#: Deliberately small. This is an integration probe, not a benchmark, and every case costs money.
LIVE_CASE_IDS = (
    "h-ordinary-factual",
    "h-multi-hop",
    "h-table-derived",
    "h-no-answer-in-corpus",
    "h-authoritative-conflict",
)


def run_live(root: Path, settings: Settings) -> dict[str, Any]:
    import json
    from uuid import uuid4

    from app.services.generation import GenerationService
    from app.sufficiency.gate import SufficiencyGate

    if settings.generator is None:
        raise DomainError(
            "GENERATION_PROVIDER_UNCONFIGURED",
            "Live evaluation needs a configured generator. Offline evaluation does not.",
            422,
        )

    gold = json.loads((root / GOLD).read_text(encoding="utf-8"))
    corpus = {"documents": gold["documents"], "chunks": gold["chunks"]}
    by_id = {case["id"]: case for case in gold["cases"]}
    gate = SufficiencyGate(settings.sufficiency)
    # The real generation service, driven at the seam that takes a prescribed EvidenceSet. That
    # keeps retrieval, PostgreSQL and Qdrant out of a probe whose subject is the provider call.
    # It still records metrics, so the probe supplies its own throwaway registry rather than a
    # stubbed service graph: the counters are real, they simply are not the server's.
    from prometheus_client import CollectorRegistry

    from app.observability.retrieval import RetrievalMetrics

    sink = SimpleNamespace(retrieval=SimpleNamespace(metrics=RetrievalMetrics(CollectorRegistry())))
    generator = GenerationService(evidence=sink, settings=settings)  # type: ignore[arg-type]

    cases: list[dict[str, Any]] = []
    for case_id in LIVE_CASE_IDS:
        case = by_id[case_id]
        evidence = build(case, corpus)
        decision = gate.evaluate(case["question"], evidence)
        record: dict[str, Any] = {
            "case": case_id,
            "category": case["category"],
            "expected_gate": case["expected_gate"],
            "actual_gate": decision.status,
            "gate_agreed": decision.status == case["expected_gate"],
            "provider_called": False,
        }
        if decision.status != "SUFFICIENT":
            # The gate refused, so no provider call is made. That suppression is the measurement.
            record["reason_codes"] = [str(c) for c in decision.reason_codes]
            record["attributed_layer"] = attribute(record["reason_codes"])
            cases.append(record)
            continue

        started = time.perf_counter()
        try:
            produced, _ = asyncio.run(
                generator._generate(case["question"], evidence, decision, uuid4())
            )
            elapsed = (time.perf_counter() - started) * 1000
            if isinstance(produced, DeclinedDraft):
                # The provider declined: the evidence does not address the question. A measured
                # outcome, not a failure, and there is no draft to inspect.
                record.update(
                    {
                        "provider_called": True,
                        "schema_valid": True,
                        "latency_ms": round(elapsed, 1),
                        "declined": True,
                        "reason_codes": ["EVIDENCE_DOES_NOT_ADDRESS_QUESTION"],
                        "attributed_layer": attribute(["EVIDENCE_DOES_NOT_ADDRESS_QUESTION"]),
                    }
                )
                cases.append(record)
                continue
            draft = produced
            supplied = {b.evidence_id for b in evidence.evidence_blocks}
            cited = {eid for claim in draft.claims for eid in claim.evidence_ids}
            record.update(
                {
                    "provider_called": True,
                    "schema_valid": True,
                    "latency_ms": round(elapsed, 1),
                    "claims": len(draft.claims),
                    "invented_citations": len(cited - supplied),
                    "citations_valid": cited <= supplied,
                    "verification_status": draft.verification_status,
                    "provider": draft.provider.provider,
                    "model_id": draft.provider.model_id,
                    "provider_ms": (draft.durations_ms or {}).get("provider_ms"),
                }
            )
        except DomainError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            record.update(
                {
                    "provider_called": True,
                    "schema_valid": False,
                    "latency_ms": round(elapsed, 1),
                    "failure_code": exc.code,
                    "attributed_layer": attribute([exc.code]),
                }
            )
        cases.append(record)

    called = [c for c in cases if c.get("provider_called")]
    latencies = [c["latency_ms"] for c in called if "latency_ms" in c]
    generator_model = settings.generator.model_id
    verifier_model = settings.verifier.model_id if settings.verifier else generator_model
    return {
        "dataset_id": gold["dataset_id"],
        "dataset_version": gold["dataset_version"],
        "independence": "HELD_OUT",
        "cases_evaluated": len(cases),
        "provider_calls": len(called),
        "provider": settings.generator.provider,
        "model_id": generator_model,
        "verifier_model_id": verifier_model,
        # Recorded rather than mentioned: with one model doing both jobs, an error the generator
        # makes is an error the verifier is disposed to repeat.
        "verifier_independent": verifier_model != generator_model,
        "gate_agreement": (sum(c["gate_agreed"] for c in cases) / len(cases) if cases else None),
        "schema_valid": all(c.get("schema_valid", True) for c in called),
        "citations_valid": all(c.get("citations_valid", True) for c in called),
        "invented_citations": sum(c.get("invented_citations", 0) for c in called),
        "suppressed_before_provider": len(cases) - len(called),
        "median_latency_ms": round(median(latencies), 1) if latencies else None,
        # Reported as unavailable rather than as zero. The M7 provider adapter does not read the
        # `usage` block off the provider response, so no token count exists to record, and a
        # summed zero would read as "this was free". Capturing usage is an M7 adapter change and
        # is deliberately not made here; it is recorded as a limitation instead.
        "token_usage_available": False,
        "total_input_tokens": None,
        "total_output_tokens": None,
        "cost_note": (
            "Cost cannot be reported: the provider adapter does not capture the response usage "
            "block, so no token counts exist. Latency is the only operational figure available, "
            "and it is a development-host measurement, not an SLO."
        ),
        "boundary": (
            f"{len(called)} live provider calls over synthetic questions. This demonstrates that "
            "the integration works. It establishes nothing about model reliability and is not "
            "clinical validation."
        ),
        "clinically_validated": False,
        "cases": cases,
    }
