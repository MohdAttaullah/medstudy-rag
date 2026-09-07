"""M11 evaluation framework: governance, taxonomy, gates and the held-out contract."""

import json
from pathlib import Path
from typing import get_args

import pytest
from app.evaluation import datasets, gates
from app.evaluation.end_to_end import GOLD, OUTCOMES, ask_outcome
from app.evaluation.manifest import model_identity, policy_fingerprints
from app.evaluation.taxonomy import (
    ATTRIBUTION,
    CORRECT_REFUSAL,
    INFRASTRUCTURE,
    LAYERS,
    SUCCESS,
    attribute,
    is_infrastructure,
    unknown_codes,
)
from app.sufficiency.model import ReasonCode as SufficiencyReason
from app.verification.model import ReasonCode as VerificationReason

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------------- taxonomy


@pytest.mark.parametrize("code", get_args(VerificationReason))
def test_every_verification_reason_code_is_classified(code):
    """M8 must not be able to declare a code the report would render as anonymous."""
    assert code in ATTRIBUTION or code in SUCCESS, code


@pytest.mark.parametrize("code", get_args(SufficiencyReason))
def test_every_sufficiency_reason_code_is_classified(code):
    assert code in ATTRIBUTION or code in SUCCESS, code


def test_attribution_prefers_the_upstream_layer():
    """A gate refusal caused by a first-stage miss is a retrieval failure, not a gate failure."""
    assert attribute(["FIRST_STAGE_MISS", "NO_EVIDENCE"]) == "RETRIEVAL"
    assert attribute(["NO_EVIDENCE"]) == "SUFFICIENCY"
    assert attribute(["CONTEXT_BUDGET_EXCEEDED", "NO_EVIDENCE"]) == "EVIDENCE"
    assert attribute(["NUMERIC_MISMATCH"]) == "VERIFICATION"
    # Reranking is only blamed when the evidence was actually in the pool.
    assert attribute(["RERANKER_REGRESSION", "UNSUPPORTED_CLAIM"]) == "RERANKING"


def test_layer_order_is_the_pipeline_order():
    assert LAYERS.index("RETRIEVAL") < LAYERS.index("RERANKING") < LAYERS.index("EVIDENCE")
    assert LAYERS.index("EVIDENCE") < LAYERS.index("SUFFICIENCY") < LAYERS.index("GENERATION")
    assert LAYERS.index("GENERATION") < LAYERS.index("VERIFICATION")


def test_success_codes_are_never_a_failure_attribution():
    assert attribute(["SUPPORTED_BY_CITED_EVIDENCE"]) is None
    assert unknown_codes(["SUPPORTED_BY_CITED_EVIDENCE"]) == ()
    assert not SUCCESS & set(ATTRIBUTION)


def test_unknown_codes_are_reported_not_swallowed():
    assert unknown_codes(["NOT_A_REAL_CODE", "NO_EVIDENCE"]) == ("NOT_A_REAL_CODE",)
    assert attribute(["NOT_A_REAL_CODE"]) is None


def test_infrastructure_failures_are_distinguishable_from_evidence_failures():
    """An outage must never be reportable as a statement about the corpus."""
    assert is_infrastructure(["GENERATION_PROVIDER_TIMEOUT"])
    assert not is_infrastructure(["NO_EVIDENCE"])
    assert not INFRASTRUCTURE & CORRECT_REFUSAL


# --------------------------------------------------------------------------------- governance


def test_every_gold_file_in_the_repository_is_governed():
    """A gold file nobody registered would appear in reports with no independence label."""
    registered = {(ROOT / d.path).resolve() for d in datasets.REGISTRY}
    found = {
        p.resolve()
        for p in list((ROOT / "backend/tests/fixtures").rglob("*gold*.json"))
        + list((ROOT / "docs/evals").glob("*.json"))
        + list((ROOT / "backend/tests/fixtures/evaluation").glob("*.json"))
    }
    assert not found - registered, f"ungoverned evaluation datasets: {found - registered}"


def test_datasets_declare_provenance_and_none_claims_clinical_validation():
    for dataset in datasets.REGISTRY:
        assert dataset.clinically_validated is False
        assert dataset.independence_meaning and dataset.review_meaning
        assert (ROOT / dataset.path).exists(), dataset.path


def test_only_a_dataset_written_after_the_policy_may_call_itself_held_out():
    held = [d for d in datasets.REGISTRY if d.independence == "HELD_OUT"]
    assert held, "M11 must contribute at least one held-out dataset"
    for dataset in held:
        assert dataset.policy_frozen_at, dataset.dataset_id
        assert dataset.milestone == "M11"


def test_no_dataset_is_expert_reviewed_and_the_manifest_says_so():
    manifest = datasets.manifest(ROOT)
    assert manifest["counts"]["expert_reviewed"] == 0
    assert manifest["clinically_validated"] is False
    assert "clinical validation" in manifest["boundary"]


def test_manifest_fingerprint_follows_the_bytes(tmp_path):
    """A silently edited gold label must change the manifest fingerprint."""
    before = datasets.manifest(ROOT)["fingerprint"]
    assert before == datasets.manifest(ROOT)["fingerprint"]
    copy = tmp_path / "heldout.json"
    original = ROOT / datasets.BY_ID["m11-heldout"].path
    copy.write_bytes(original.read_bytes() + b"\n")
    assert datasets.file_fingerprint(copy) != datasets.file_fingerprint(original)


# ------------------------------------------------------------------------------------- gates


def test_a_safety_invariant_that_was_not_measured_does_not_pass():
    """Not measured is not upheld. This is the line that makes the whole report honest."""
    verdict = gates.evaluate_all({"layers": {}, "unclassified_reason_codes": []})
    assert verdict["failed"] == []
    assert verdict["safety_invariants_not_measured"]
    assert verdict["passed"] is False


def test_a_failing_safety_invariant_fails_the_run():
    report = {
        "layers": {
            "verification": {"false_pass_count": 1, "repair_cap_respected": True},
            "sufficiency": {"false_allow_count": 0},
            "generation": {"invented_citation_count": 0},
            "end_to_end": {
                "answer_without_verified_count": 0,
                "answered_when_unanswerable_count": 0,
            },
            "security": {"violations": []},
        },
        "unclassified_reason_codes": [],
    }
    verdict = gates.evaluate_all(report)
    assert "verification.no_false_pass" in verdict["failed"]
    assert verdict["passed"] is False


def test_every_gate_declares_how_much_authority_it_has():
    kinds = {gate.kind for gate in gates.GATES}
    assert kinds <= set(get_args(gates.Kind))
    for gate in gates.GATES:
        assert gate.kind_meaning
        if gate.kind in {"OBSERVATIONAL_METRIC", "UNCALIBRATED"}:
            assert not gate.asserts_a_bound, f"{gate.gate_id} must not assert a bound"
        if gate.kind == "HARD_SAFETY_INVARIANT":
            assert gate.maximum == 0, f"{gate.gate_id} must be a zero-tolerance bound"


def test_uncalibrated_metrics_exist_and_answer_correctness_is_one_of_them():
    """Medical correctness has no expert-reviewed dataset here, so it must not become a gate."""
    correctness = next(g for g in gates.GATES if g.gate_id == "end_to_end.answer_correctness")
    assert correctness.kind == "UNCALIBRATED"
    assert not correctness.asserts_a_bound


# ------------------------------------------------------------------------------- held-out set


def _gold() -> dict:
    return json.loads((ROOT / GOLD).read_text(encoding="utf-8"))


REQUIRED_CATEGORIES = (
    "ordinary factual",
    "multi-hop",
    "table-derived",
    "table fragment",
    "formula-derived",
    "figure requiring",
    "question-bank evidence",
    "answer key contradicting",
    "two authoritative sources agreeing",
    "two authoritative sources disagreeing",
    "outdated edition",
    "answer genuinely absent",
    "ambiguous question",
    "single truncated fragment",
    "evidence omitted by the context budget",
    "numeric claim disagreeing",
    "claim reversing the polarity",
    "material claim carrying no citation",
    "draft citing evidence that was never supplied",
    "verifier judging a claim unsupported",
    "verifier itself failing",
    "repair attempt",
    "generation provider outage",
)


@pytest.mark.parametrize("category", REQUIRED_CATEGORIES)
def test_required_case_categories_are_all_covered(category):
    categories = " | ".join(case["category"] for case in _gold()["cases"])
    assert category in categories, category


def test_held_out_gold_states_its_own_limits():
    gold = _gold()
    assert gold["clinically_validated"] is False
    assert gold["policy_frozen_at"]
    for phrase in ("held out", "no clinician", "synthetic"):
        assert phrase in gold["notice"].lower(), phrase


def test_every_held_out_case_declares_a_reachable_outcome():
    for case in _gold()["cases"]:
        assert case["expected_outcome"] in OUTCOMES, case["id"]
        assert case["expected_gate"] in {"SUFFICIENT", "INSUFFICIENT", "CONFLICTING"}
        # An unanswerable case that expects a verified answer would be a contradictory label.
        assert not (case["expected_outcome"] == "VERIFIED" and not case["answerable"]), case["id"]


def test_ask_outcome_matches_the_product_mapping_on_every_reachable_state():
    """The evaluation must call a refusal exactly what the Ask contract calls it.

    `AskService._outcome` is re-implemented in the evaluator because importing the service would
    drag PostgreSQL into an offline harness. This test is what keeps the copy honest.
    """
    from app.services.ask import AskService

    for status in ("SUFFICIENT", "INSUFFICIENT", "CONFLICTING", None):
        for verified in (True, False):
            payload = {
                "verified": verified,
                "verified_answer": {"answer": "x"} if verified else None,
                "sufficiency": {"status": status} if status else {},
            }
            assert ask_outcome(
                verified=verified, sufficiency_status=status, provider_failed=False
            ) == AskService._outcome(payload)
    # The one state the product reaches through an exception rather than a payload.
    assert ask_outcome(verified=False, sufficiency_status="SUFFICIENT", provider_failed=True) == (
        "FAILED"
    )


# ----------------------------------------------------------------------------------- secrets


def test_run_manifest_records_model_identity_but_never_a_credential():
    from app.core.config import Settings

    settings = Settings.model_construct()
    identity = model_identity(settings)
    serialized = json.dumps(identity)
    assert "api_key" not in serialized and "sk-" not in serialized
    assert set(identity["credentials_present"]) == {"openai", "anthropic"}
    assert all(isinstance(v, bool) for v in identity["credentials_present"].values())
    assert "verifier_independent_of_generator" in identity


def test_policy_fingerprints_cover_every_layer_that_can_move_a_number():
    from app.core.config import Settings

    fingerprints = policy_fingerprints(Settings.model_construct())
    for group in (
        "retrieval",
        "reranking",
        "expansion",
        "evidence_budget",
        "sufficiency",
        "claim_verification",
        "final_verification",
        "chunking",
        "embedding",
    ):
        assert group in fingerprints, group
        assert len(fingerprints[group]) >= 16
