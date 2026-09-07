"""Runs every evaluation layer and aggregates them without merging them.

There is deliberately no overall score. A parsing fidelity figure, a Recall@5 and a false-PASS
count are not commensurable, and averaging them would produce a number whose movement nobody could
explain — while hiding the one quantity that actually matters underneath fifteen that do not.
Each layer keeps its own dataset, its own metrics and its own limitations, and the aggregate is a
table of contents rather than a verdict.

A layer that cannot run is recorded as NOT_RUN with the reason, never silently omitted. That
distinction is carried into the gates: a safety invariant that was not measured has not been
upheld, and `gates.evaluate_all` refuses to call the run passed on that basis.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from app.core.config import Settings
from app.evaluation import datasets, gates, manifest

#: Layers whose evaluation needs neural weights or Docling. They are skipped by default so the
#: standard run stays fast and hermetic, and are enabled with --include-models.
MODEL_BACKED = frozenset({"parsing", "indexing", "retrieval", "reranking"})


def _script(root: Path, name: str, *args: str, timeout: int = 3600) -> dict[str, Any]:
    """Run one existing per-layer evaluation script and read back its JSON report."""
    out = root / ".local" / f"m11-{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [sys.executable, str(root / "scripts" / f"evaluate_{name}.py"), "--json", str(out), *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if done.returncode != 0 or not out.exists():
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"evaluate_{name}.py exited {done.returncode}: {' | '.join(tail)}")
    loaded: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return loaded


# --------------------------------------------------------------------------- layer runners


def _chunking(root: Path, settings: Settings) -> dict[str, Any]:
    from app.evaluation.chunking import describe, evaluate, load_gold
    from app.ingestion.chunking.tokenizer import LocalTokenizer

    config = settings.chunking
    gold = load_gold(root / datasets.BY_ID["chunking-gold"].path)
    described = describe(evaluate(gold, config, LocalTokenizer(config)))
    described["cases_evaluated"] = len(gold)
    described["failure_count"] = len(described.get("failures", []))
    described["policy_fingerprint"] = config.fingerprint
    described["dataset_version"] = datasets.BY_ID["chunking-gold"].version
    return described


def _evidence(root: Path, settings: Settings) -> dict[str, Any]:
    from app.evaluation.evidence import evaluate_context

    report = evaluate_context(root)
    # The sweep measures several expansion/budget policies. Only the one the system is actually
    # configured with may be quoted as its coverage; reporting the best of the sweep would
    # describe a configuration nobody is running.
    key = (
        f"neighbours-{settings.expansion.previous_siblings}"
        f"-budget-{settings.evidence_budget.max_total_tokens}"
    )
    effective = (report.get("policies") or {}).get(key)
    report["effective_policy"] = key
    report["element_coverage"] = effective["mean_coverage"] if effective else None
    report["character_coverage"] = effective["mean_character_coverage"] if effective else None
    report["noise_ratio"] = effective["mean_noise_ratio"] if effective else None
    report["dataset_version"] = report.get("version") or datasets.BY_ID["context-gold"].version
    report["expansion_policy_fingerprint"] = settings.expansion.fingerprint
    report["budget_policy_fingerprint"] = settings.evidence_budget.fingerprint
    return report


def _sufficiency(root: Path, settings: Settings) -> dict[str, Any]:
    from app.evaluation.sufficiency import evaluate_sufficiency

    return evaluate_sufficiency(root, settings.sufficiency)


def _generation(root: Path, settings: Settings) -> dict[str, Any]:
    from app.evaluation.sufficiency import evaluate_generation

    report = evaluate_generation(root, settings.sufficiency)
    report.setdefault("invented_citation_count", report.get("invented_evidence_ids", 0))
    # The draft path is measured over the M7 gate corpus, so it inherits that dataset's identity.
    report.setdefault("dataset_version", datasets.BY_ID["sufficiency-gold"].version)
    return report


def _verification(root: Path, settings: Settings) -> dict[str, Any]:
    from app.evaluation.verification import evaluate

    return evaluate(root)


def _end_to_end(root: Path, settings: Settings) -> dict[str, Any]:
    from app.evaluation.end_to_end import evaluate

    return evaluate(root, settings.sufficiency)


def _security(root: Path, settings: Settings) -> dict[str, Any]:
    """Contract-level security invariants that need no database.

    Cross-tenant isolation over real PostgreSQL rows is proven by the M9 and M10 integration
    suites, not here; what this checks is the set of invariants that live in types and role
    definitions, where a regression would otherwise only surface at runtime.
    """
    from uuid import UUID

    from pydantic import ValidationError

    from app.configuration.registry import REGISTRY, public_snapshot
    from app.schemas.ask import AskResponse
    from app.security.auth import ROLE_PERMISSIONS

    violations: list[str] = []
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "passed": ok, "detail": detail})
        if not ok:
            violations.append(name)

    # Every field is supplied correctly except the one under test, so a rejection can only come
    # from the display rule. An earlier version passed unknown keyword arguments and was rejected
    # for that instead -- it would have reported success even with the rule removed.
    blank = UUID(int=0)

    def build_response(outcome: str, verified: bool, answer: str | None) -> AskResponse:
        """Every field correct except the ones under test, so only the display rule can reject."""
        return AskResponse(
            correlation_id=blank,
            conversation_id=blank,
            turn_id=blank,
            question="probe",
            message="probe",
            created_at="1970-01-01T00:00:00+00:00",
            outcome=cast(Any, outcome),
            verified=verified,
            answer=answer,
        )

    # An earlier version of this check passed unknown keyword arguments and was rejected for that
    # instead of by the rule -- it would have reported success even with the rule removed.
    try:
        build_response("UNVERIFIED", False, "unverified draft")
        leaked = True
    except (ValidationError, ValueError, TypeError):
        leaked = False
    check(
        "unverified_answer_unconstructable",
        not leaked,
        "AskResponse refuses an answer on a non-VERIFIED outcome.",
    )
    # The control: the same construction with a VERIFIED outcome must succeed, so the check above
    # cannot pass merely because the model rejects everything.
    try:
        build_response("VERIFIED", True, "verified answer")
        constructible = True
    except (ValidationError, ValueError, TypeError):
        constructible = False
    check(
        "verified_answer_is_still_constructible",
        constructible,
        "The display rule refuses unverified answers without refusing verified ones.",
    )

    reader = ROLE_PERMISSIONS["reader"]
    for scope in (
        "settings:write",
        "settings:read",
        "retrieval:search",
        "generation:draft",
        "generation:verify",
        "audit:read",
    ):
        check(
            f"reader_lacks_{scope.replace(':', '_')}",
            scope not in reader,
            f"reader does not hold {scope}.",
        )

    snapshot = public_snapshot(settings)
    secrets = {
        "openai_api_key": settings.openai_api_key.get_secret_value(),
        "anthropic_api_key": settings.anthropic_api_key.get_secret_value(),
        "database_url": settings.database_url.get_secret_value(),
    }
    serialized = json.dumps(snapshot)
    for name, value in secrets.items():
        check(
            f"no_{name}_in_settings_snapshot",
            not value or value not in serialized,
            "Configured secret values never appear in the settings projection.",
        )
    check(
        "no_secret_keys_registered",
        not any(k.endswith("_api_key") or k == "database_url" for k in REGISTRY),
        "No credential field is a registered, editable setting.",
    )
    check(
        "credentials_are_presence_only",
        all(isinstance(snapshot[k], bool) for k in REGISTRY if k.startswith("credentials.")),
        "Credential settings project presence, never a value.",
    )

    return {
        "dataset_id": None,
        "independence": "N/A",
        "checks": checks,
        "checks_run": len(checks),
        "violations": violations,
        "note": (
            "Contract-level only. Cross-tenant retrieval, conversation and citation isolation over "
            "real database rows are proven by the M9/M10 integration suites."
        ),
    }


def _retrieval(root: Path, settings: Settings) -> dict[str, Any]:
    report = _script(root, "retrieval")
    lanes = report.get("lanes") or {}
    # Flatten the three lanes so each is reported on its own. A single "retrieval" number would
    # hide that dense and BM25 fail on different questions, which is the whole point of hybrid.
    summary: dict[str, Any] = {}
    for lane, payload in lanes.items():
        recall = payload.get("recall") or {}
        summary[lane] = {
            "scored_cases": payload.get("scored_cases"),
            "mrr": payload.get("mrr"),
            "recall_at_1": recall.get("@1"),
            "recall_at_3": recall.get("@3"),
            "recall_at_5": recall.get("@5"),
            "recall_at_10": recall.get("@10"),
            "recall_at_20": recall.get("@20"),
            "ndcg_at_5": (payload.get("ndcg") or {}).get("@5"),
            "ndcg_at_10": (payload.get("ndcg") or {}).get("@10"),
            "precision_at_5": (payload.get("precision") or {}).get("@5"),
            "per_category": payload.get("per_category"),
        }
    report["lane_summary"] = summary
    hybrid = summary.get("HYBRID_RRF", {})
    report["hybrid_recall_at_5"] = hybrid.get("recall_at_5")
    report["cases_evaluated"] = report.get("scored_cases")
    # Candidate-pool recall is measured at the depth M6 actually draws from, so a "miss" here
    # means the evidence was never available to the reranker rather than that it ranked it low.
    depth = settings.reranking.candidate_top_k
    at_pool = hybrid.get("recall_at_20") if depth >= 20 else hybrid.get("recall_at_10")
    report["candidate_pool_depth"] = depth
    report["candidate_pool_recall"] = at_pool
    report["first_stage_miss_rate"] = None if at_pool is None else round(1.0 - at_pool, 4)
    return report


def _reranking(root: Path, settings: Settings) -> dict[str, Any]:
    report = _script(root, "reranking")
    pool = report.get("pools", {}).get(str(settings.reranking.candidate_top_k)) or {}
    queries = pool.get("per_query") or []
    # The two failures kept apart on purpose: evidence the first stage never supplied is not a
    # reranker failure, and blaming M6 for it would send the next investigation to the wrong place.
    classes = [c for q in queries for c in (q.get("failure_classes") or [])]
    report["dataset_version"] = datasets.BY_ID["retrieval-gold"].version
    report["cases_evaluated"] = len(queries)
    report["candidate_pool_depth"] = settings.reranking.candidate_top_k
    report["candidate_pool_recall"] = pool.get("candidate_pool_recall")
    report["baseline"] = (pool.get("baseline") or {}).get("recall")
    report["reranked"] = (pool.get("reranked") or {}).get("recall")
    report["baseline_mrr"] = (pool.get("baseline") or {}).get("mrr")
    report["reranked_mrr"] = (pool.get("reranked") or {}).get("mrr")
    report["baseline_ndcg_at_5"] = ((pool.get("baseline") or {}).get("ndcg") or {}).get("@5")
    report["reranked_ndcg_at_5"] = ((pool.get("reranked") or {}).get("ndcg") or {}).get("@5")
    report["first_stage_miss_count"] = classes.count("FIRST_STAGE_MISS")
    report["reranker_regression_count"] = classes.count("RERANKER_REGRESSION")
    report["reranker_no_gain_count"] = classes.count("RERANKER_NO_GAIN")
    report["inference_mean_ms"] = pool.get("inference_mean_ms")
    return report


RUNNERS: tuple[tuple[str, Callable[[Path, Settings], dict[str, Any]]], ...] = (
    ("parsing", lambda root, s: _script(root, "parsing")),
    ("chunking", _chunking),
    ("indexing", lambda root, s: _script(root, "embeddings")),  # M4 technical validation
    ("retrieval", _retrieval),
    ("reranking", _reranking),
    ("evidence", _evidence),
    ("sufficiency", _sufficiency),
    ("generation", _generation),
    ("verification", _verification),
    ("end_to_end", _end_to_end),
    ("security", _security),
)


def _label(payload: dict[str, Any]) -> None:
    by_version = {d.version: d for d in datasets.REGISTRY}
    by_path_id = {d.dataset_id: d for d in datasets.REGISTRY}
    known = by_version.get(str(payload.get("dataset_version"))) or by_path_id.get(
        str(payload.get("dataset_id"))
    )
    if known is None:
        return
    payload.setdefault("dataset_id", known.dataset_id)
    payload["independence"] = known.independence
    payload["independence_meaning"] = known.independence_meaning
    payload["review"] = known.review


def run(
    root: Path,
    settings: Settings,
    *,
    include_models: bool = False,
    only: frozenset[str] | None = None,
) -> dict[str, Any]:
    dataset_manifest = datasets.manifest(root)
    report: dict[str, Any] = {
        "run": manifest.build(root, settings, "offline", dataset_manifest),
        "dataset_manifest": dataset_manifest,
        "layers": {},
        "layer_status": {},
    }
    for name, runner in RUNNERS:
        if only is not None and name not in only:
            report["layer_status"][name] = {"status": "SKIPPED", "reason": "not selected"}
            continue
        if name in MODEL_BACKED and not include_models:
            report["layer_status"][name] = {
                "status": "NOT_RUN",
                "reason": "needs neural weights or Docling; enable with --include-models",
            }
            continue
        started = time.perf_counter()
        try:
            payload = runner(root, settings)
            # Governance travels with the numbers. A layer report that names its dataset version
            # gets that dataset's independence class attached, so no table can show a figure
            # without showing what kind of evidence it is.
            _label(payload)
            report["layers"][name] = payload
            report["layer_status"][name] = {
                "status": "RUN",
                "seconds": round(time.perf_counter() - started, 2),
            }
        except Exception as exc:  # noqa: BLE001 - a broken layer is data, not a crash
            report["layer_status"][name] = {
                "status": "NOT_RUN",
                "reason": f"{type(exc).__name__}: {exc}"[:400],
                "seconds": round(time.perf_counter() - started, 2),
            }

    report["unclassified_reason_codes"] = sorted(
        {
            code
            for layer in report["layers"].values()
            if isinstance(layer, dict)
            for code in layer.get("unknown_reason_codes", [])
        }
    )
    report["quality_gates"] = gates.evaluate_all(report)
    report["clinically_validated"] = False
    report["boundary"] = (
        "M11 engineering evaluation is not clinical validation. Every dataset here is synthetic "
        "and none is expert-reviewed."
    )
    return report
