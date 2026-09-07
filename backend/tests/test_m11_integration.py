"""M11 harness end to end: the real layers, the real gate, and the offline guarantee.

These need no PostgreSQL, no Qdrant and no API key — which is the point. If the default evaluation
needed infrastructure or a paid provider, it would stop being run, and an evaluation nobody runs
measures nothing.
"""

import json
from pathlib import Path

import pytest
from app.core.config import Settings
from app.evaluation import harness
from app.evaluation.end_to_end import evaluate as evaluate_end_to_end
from app.evaluation.report import render

ROOT = Path(__file__).resolve().parents[2]

#: Layers that run without neural weights or Docling. The model-backed layers are exercised by
#: their own milestone suites and by the --include-models run recorded in the M11 report.
OFFLINE_LAYERS = frozenset(
    {"chunking", "evidence", "sufficiency", "generation", "verification", "end_to_end", "security"}
)


@pytest.fixture(scope="module")
def report() -> dict:
    return harness.run(ROOT, Settings.model_construct(), only=OFFLINE_LAYERS)


def test_offline_run_makes_no_provider_call(monkeypatch):
    """The default evaluation must never spend money, and this is what proves it.

    Any HTTP request at all fails the test: the offline path has no legitimate reason to open a
    socket, so an attempt is a defect rather than a detail.
    """
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("offline evaluation attempted a network call")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    result = harness.run(ROOT, Settings.model_construct(), only=OFFLINE_LAYERS)
    assert result["quality_gates"]["passed"] is True


def test_offline_run_needs_no_api_key():
    settings = Settings.model_construct()
    assert not settings.openai_api_key.get_secret_value()
    result = harness.run(ROOT, settings, only=OFFLINE_LAYERS)
    assert result["quality_gates"]["safety_invariants_upheld"] == 7


def test_every_hard_safety_invariant_is_measured_offline(report):
    verdict = report["quality_gates"]
    assert verdict["safety_invariants_not_measured"] == []
    assert verdict["safety_invariants_upheld"] == verdict["safety_invariants_total"]
    assert verdict["failed"] == []
    assert verdict["passed"] is True


def test_layers_stay_separate_and_no_master_score_is_produced(report):
    """A single 'RAG accuracy' number would hide which stage failed. There must not be one."""
    assert set(report["layers"]) <= set(dict(harness.RUNNERS))
    for forbidden in ("overall_score", "accuracy", "score", "total_score"):
        assert forbidden not in report
    # Each layer keeps its own dataset identity rather than inheriting a shared one.
    identities = {
        name: payload.get("dataset_version")
        for name, payload in report["layers"].items()
        if name != "security"
    }
    assert len(set(identities.values())) > 1


def test_a_layer_that_cannot_run_is_recorded_not_omitted():
    result = harness.run(ROOT, Settings.model_construct(), only=frozenset({"sufficiency"}))
    skipped = [n for n, s in result["layer_status"].items() if s["status"] == "SKIPPED"]
    assert skipped, "unselected layers must still appear with a status"
    # And a run missing a safety layer must not claim to have passed.
    assert result["quality_gates"]["passed"] is False


def test_every_layer_report_carries_its_independence_class(report):
    for name, payload in report["layers"].items():
        assert "independence" in payload, name
        assert payload["independence"] in {
            "IMPLEMENTATION_ADJACENT",
            "FROZEN_REGRESSION",
            "HELD_OUT",
            "N/A",
        }


def test_run_manifest_identifies_code_policy_and_data(report):
    run = report["run"]
    assert run["git"]["commit"]
    assert run["dataset_manifest_fingerprint"]
    assert run["policy_fingerprints"]["sufficiency"]
    assert run["policy_fingerprints"]["retrieval"]
    assert run["clinically_validated"] is False
    serialized = json.dumps(report, default=str)
    assert "sk-" not in serialized
    for credential in ("openai_api_key", "anthropic_api_key"):
        assert f'"{credential}"' not in serialized


def test_end_to_end_preserves_upstream_attribution(report):
    """A failure must name the stage that caused it, never a generic FAILED."""
    end_to_end = report["layers"]["end_to_end"]
    assert end_to_end["layer_attribution_agreement"] == 1.0
    assert end_to_end["unknown_reason_codes"] == []
    # The provider-outage case must be attributed to generation, not to the evidence.
    outage = next(c for c in end_to_end["cases"] if c["case"] == "h-provider-failure")
    assert outage["actual_outcome"] == "FAILED"
    assert outage["attributed_layer"] == "GENERATION"
    # And the corpus-lacks-evidence case must not be reported as a technical failure.
    absent = next(c for c in end_to_end["cases"] if c["case"] == "h-no-answer-in-corpus")
    assert absent["actual_outcome"] == "INSUFFICIENT_EVIDENCE"
    assert absent["attributed_layer"] == "SUFFICIENCY"


def test_held_out_set_never_answers_an_unanswerable_question(report):
    end_to_end = report["layers"]["end_to_end"]
    assert end_to_end["answered_when_unanswerable_count"] == 0
    assert end_to_end["answer_without_verified_count"] == 0
    for case in end_to_end["cases"]:
        if case["actual_outcome"] != "VERIFIED":
            assert case["answer"] is None, case["case"]


def test_verification_failures_are_not_reported_as_missing_evidence(report):
    """A rejected draft and an empty corpus are different failures and must stay different."""
    cases = {c["case"]: c for c in report["layers"]["end_to_end"]["cases"]}
    for case_id in ("h-numeric-mismatch", "h-negation-reversed", "h-verifier-unavailable"):
        case = cases[case_id]
        assert case["actual_outcome"] == "UNVERIFIED", case_id
        assert case["attributed_layer"] == "VERIFICATION", case_id


def test_conflicting_sources_abstain_rather_than_choosing_a_winner(report):
    cases = {c["case"]: c for c in report["layers"]["end_to_end"]["cases"]}
    for case_id in ("h-authoritative-conflict", "h-outdated-source", "h-answer-key-conflict"):
        case = cases[case_id]
        assert case["actual_outcome"] == "CONFLICTING_EVIDENCE", case_id
        assert case["answer"] is None, case_id


def test_the_configured_evidence_policy_is_the_one_reported(report):
    """Quoting the best policy of a sweep would describe a configuration nobody is running."""
    settings = Settings.model_construct()
    evidence = report["layers"]["evidence"]
    assert evidence["effective_policy"] == (
        f"neighbours-{settings.expansion.previous_siblings}"
        f"-budget-{settings.evidence_budget.max_total_tokens}"
    )
    assert 0.0 <= evidence["element_coverage"] <= 1.0


def test_rendered_report_shows_the_boundary_and_the_independence_labels(report):
    text = render(report)
    assert "not clinical validation" in text
    assert "HELD_OUT" in text and "IMPLEMENTATION_ADJACENT" in text
    assert "HARD_SAFETY_INVARIANT" in text
    # No aggregate verdict masquerading as a quality score.
    assert "Overall accuracy" not in text


def test_end_to_end_evaluation_is_deterministic():
    settings = Settings.model_construct()
    first = evaluate_end_to_end(ROOT, settings.sufficiency)
    second = evaluate_end_to_end(ROOT, settings.sufficiency)
    assert [c["actual_outcome"] for c in first["cases"]] == [
        c["actual_outcome"] for c in second["cases"]
    ]
    assert first["outcome_agreement"] == second["outcome_agreement"]
