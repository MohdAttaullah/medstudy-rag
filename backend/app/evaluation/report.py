"""Renders the aggregate report for humans, one layer at a time.

Every section names its dataset and that dataset's independence class next to the numbers, because
"98%" means something entirely different on a fixture written alongside the code than on a case the
policy never saw. Keeping the label adjacent to the figure is the only reliable way to stop the
figure from travelling without it.
"""

from typing import Any

_LAYER_TITLES = {
    "parsing": "Parsing",
    "chunking": "Chunking",
    "indexing": "Embedding / index",
    "retrieval": "Retrieval",
    "reranking": "Reranking",
    "evidence": "EvidenceSet",
    "sufficiency": "Evidence sufficiency",
    "generation": "Generation",
    "verification": "Claim verification",
    "end_to_end": "End-to-end Ask",
    "security": "Security",
    "live": "Live provider",
}

#: What each layer is worth reporting. Keys absent from a layer are skipped silently, so a layer
#: can grow a metric without this module needing to know about it first.
_LAYER_METRICS: dict[str, tuple[str, ...]] = {
    "parsing": ("cases_evaluated", "passed_cases", "checks", "failure_count", "parser"),
    "chunking": ("cases_evaluated", "checks", "passed_cases", "failure_count"),
    "indexing": (
        "cases_evaluated",
        "passed_cases",
        "checks",
        "failure_count",
        "dimension",
        "pooling",
        "distance",
        "vector_space_fingerprint",
    ),
    "retrieval": (
        "cases_evaluated",
        "candidate_pool_depth",
        "candidate_pool_recall",
        "first_stage_miss_rate",
        "hybrid_recall_at_5",
    ),
    "reranking": (
        "cases_evaluated",
        "candidate_pool_depth",
        "candidate_pool_recall",
        "baseline_mrr",
        "reranked_mrr",
        "baseline_ndcg_at_5",
        "reranked_ndcg_at_5",
        "first_stage_miss_count",
        "reranker_regression_count",
        "reranker_no_gain_count",
        "inference_mean_ms",
    ),
    "evidence": (
        "effective_policy",
        "element_coverage",
        "character_coverage",
        "noise_ratio",
    ),
    "sufficiency": (
        "cases_evaluated",
        "agreement",
        "false_allow_count",
        "unnecessary_abstention_count",
        "abstention_rate",
    ),
    "generation": (
        "cases_evaluated",
        "generation_attempted",
        "generation_suppressed",
        "schema_valid_rate",
        "citation_valid_rate",
        "invented_citation_count",
        "rank_or_score_leaked_to_provider",
    ),
    "verification": (
        "cases_evaluated",
        "agreement",
        "false_pass_count",
        "unsupported_claim_detection_rate",
        "citation_failures_detected",
        "contradiction_cases_detected",
        "repair_attempted",
        "repair_success_rate",
        "post_repair_unsupported",
        "repair_cap_respected",
    ),
    "end_to_end": (
        "cases_evaluated",
        "gate_agreement",
        "outcome_agreement",
        "abstention_rate",
        "answered_when_unanswerable_count",
        "answer_without_verified_count",
        "refused_when_answerable_count",
        "layer_attribution_agreement",
        "repair_cap_respected",
    ),
    "security": ("checks_run", "violations"),
    "live": (
        "cases_evaluated",
        "provider",
        "model_id",
        "verifier_model_id",
        "verifier_independent",
        "schema_valid",
        "citations_valid",
        "invented_citations",
        "verified_count",
        "median_latency_ms",
        "suppressed_before_provider",
        "token_usage_available",
        "cost_note",
    ),
}


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "none"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items()) if value else "none"
    return "—" if value is None else str(value)


def render(report: dict[str, Any]) -> str:
    run = report.get("run", {})
    git = run.get("git", {})
    models = run.get("models", {})
    manifest = report.get("dataset_manifest", {})
    lines: list[str] = []
    add = lines.append

    add("# M11 evaluation report")
    add("")
    add(
        "**M11 engineering evaluation is not clinical validation.** Every dataset below is "
        "synthetic, none is expert-reviewed, and no number here is evidence about clinical "
        "performance. There is deliberately no single accuracy score: the layers measure "
        "different things and are not commensurable."
    )
    add("")

    add("## Run identity")
    add("")
    add("| Field | Value |")
    add("|---|---|")
    add(f"| Run id | `{run.get('run_id', '—')}` |")
    add(f"| Mode | {run.get('mode', '—')} |")
    add(f"| Commit | `{git.get('commit', '—')}` ({'dirty' if git.get('dirty') else 'clean'}) |")
    add(f"| Timestamp | {run.get('timestamp', '—')} |")
    add(f"| Configuration revision | {_fmt(run.get('configuration_revision'))} |")
    add(f"| Dataset manifest | `{manifest.get('fingerprint', '—')[:16]}` |")
    generator = models.get("generator")
    verifier = models.get("verifier")
    add(f"| Generator | {_fmt(generator and generator['model_id'])} |")
    # An unset verifier is not "unknown": it means M8 reuses the generator, which is the whole
    # reason `verifier_independent_of_generator` is false. Say that rather than printing a dash.
    add(
        "| Verifier | "
        + (
            _fmt(verifier["model_id"])
            if verifier
            else (
                f"not configured — M8 reuses the generator ({_fmt(generator['model_id'])})"
                if generator
                else "—"
            )
        )
        + " |"
    )
    independent = _fmt(models.get("verifier_independent_of_generator"))
    add(f"| Verifier independent of generator | {independent} |")
    add("")

    add("## Datasets")
    add("")
    add("| Dataset | Version | Layer | Independence | Review |")
    add("|---|---|---|---|---|")
    for dataset in manifest.get("datasets", []):
        add(
            f"| {dataset['dataset_id']} | `{dataset['version']}` | {dataset['layer']} "
            f"| {dataset['independence']} | {dataset['review']} |"
        )
    counts = manifest.get("counts", {})
    add("")
    add(
        f"{counts.get('IMPLEMENTATION_ADJACENT', 0)} implementation-adjacent, "
        f"{counts.get('FROZEN_REGRESSION', 0)} frozen regression, "
        f"{counts.get('HELD_OUT', 0)} held out, "
        f"{counts.get('expert_reviewed', 0)} expert-reviewed."
    )
    add("")

    add("## Layers")
    add("")
    for name, title in _LAYER_TITLES.items():
        status = report.get("layer_status", {}).get(name)
        payload = report.get("layers", {}).get(name)
        if status is None and payload is None:
            continue
        add(f"### {title}")
        add("")
        if payload is None:
            state = (status or {}).get("status", "NOT_RUN")
            add(f"*{state}* — {(status or {}).get('reason', 'no reason recorded')}")
            add("")
            continue
        dataset = payload.get("dataset_id") or payload.get("dataset_version") or "—"
        independence = payload.get("independence") or "—"
        add(f"Dataset `{dataset}` · independence **{independence}**")
        add("")
        add("| Metric | Value |")
        add("|---|---|")
        for key in _LAYER_METRICS.get(name, ()):  # noqa: B905
            if key in payload:
                add(f"| {key.replace('_', ' ')} | {_fmt(payload[key])} |")
        add("")
        if name == "retrieval" and payload.get("lane_summary"):
            add("Per lane, reported separately:")
            add("")
            add("| Lane | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@5 |")
            add("|---|---|---|---|---|---|")
            for lane, metrics in payload["lane_summary"].items():
                add(
                    f"| {lane} | {_fmt(metrics['recall_at_1'])} | {_fmt(metrics['recall_at_3'])} "
                    f"| {_fmt(metrics['recall_at_5'])} | {_fmt(metrics['mrr'])} "
                    f"| {_fmt(metrics['ndcg_at_5'])} |"
                )
            add("")
        if payload.get("failure_layers"):
            add(f"Failures attributed upstream: {_fmt(payload['failure_layers'])}")
            add("")

    add("## Quality gates")
    add("")
    add("| Gate | Kind | Observed | Bound | Status |")
    add("|---|---|---|---|---|")
    for gate in report.get("quality_gates", {}).get("gates", []):
        bound = (
            f"≤ {gate['maximum']}"
            if gate["maximum"] is not None
            else f"≥ {gate['minimum']}"
            if gate["minimum"] is not None
            else "none asserted"
        )
        add(
            f"| `{gate['gate_id']}` | {gate['kind']} | {_fmt(gate['observed'])} "
            f"| {bound} | {gate['status']} |"
        )
    verdict = report.get("quality_gates", {})
    add("")
    add(
        f"Hard safety invariants: **{verdict.get('safety_invariants_upheld', 0)}"
        f"/{verdict.get('safety_invariants_total', 0)} upheld**. "
        f"Failed gates: {_fmt(verdict.get('failed'))}."
    )
    if verdict.get("safety_invariants_not_measured"):
        add("")
        add(
            "**A safety invariant was not measured.** Not measured is not upheld; this run does "
            f"not pass: {_fmt(verdict['safety_invariants_not_measured'])}"
        )
    add("")

    add("## Boundary")
    add("")
    add(
        "No claim of clinical validation, medical certification, guaranteed accuracy or absence "
        "of hallucination is made or implied by anything above. Establishing medical quality "
        "requires expert-reviewed datasets that this repository does not have."
    )
    return "\n".join(lines)
