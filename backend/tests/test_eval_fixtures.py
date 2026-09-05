import json
from pathlib import Path


def test_synthetic_contract_cases_have_consistent_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    dataset = json.loads((root / "docs/evals/bootstrap-cases.json").read_text())
    assert dataset["clinically_validated"] is False
    evidence_ids = {item["id"] for item in dataset["evidence"]}
    assert len(evidence_ids) == len(dataset["evidence"])
    question_ids = set()
    for case in dataset["questions"]:
        assert case["id"] not in question_ids
        question_ids.add(case["id"])
        assert set(case["relevant_ids"]) <= evidence_ids
        if case["expected_gate"] == "SUFFICIENT":
            assert case["relevant_ids"]
