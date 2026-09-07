"""Measure M8 claim verification. Deterministic doubles only; never calls a provider."""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.verification_config import (  # noqa: E402
    ClaimExtractionConfig,
    ClaimVerificationConfig,
    ContradictionConfig,
    FinalVerificationConfig,
    RepairConfig,
)
from app.evaluation.verification import GOLD, evaluate  # noqa: E402


def commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def markdown(report: dict) -> str:
    result = report["verification"]
    lines = [
        "# M8 claim-verification evaluation",
        "",
        "Synthetic engineering benchmark. Drafts are paired directly with the evidence they cite, "
        "so verification is measured independently of retrieval, the sufficiency gate and "
        "generation. No clinician annotations and no clinical accuracy claim.",
        "",
        f"Dataset `{result['dataset_version']}`; {result['cases_evaluated']} cases.",
        "",
        "| Outcome | Count |",
        "|---|---|",
    ]
    for key, value in result["confusion"].items():
        lines.append(f"| {key.replace('->', ' → ')} | {value} |")
    lines += [
        "",
        f"**False PASS (unsupported answer released as verified): "
        f"{result['false_pass_count']} — rate {result['false_pass_rate']:.4f}.** "
        f"{result['false_passes'] or 'none'}.",
        "",
        "This is the metric that matters. A false PASS emits a wrong medical answer; an "
        "unnecessary abstention emits nothing. They are not interchangeable and are never averaged "
        "together.",
        "",
        f"Agreement with the labelled outcome: {result['agreement']:.4f}. "
        f"Abstention rate: {result['abstention_rate']:.4f}. "
        f"Unnecessary abstentions: {result['unnecessary_abstentions'] or 'none'}.",
        "",
        f"Unsupported-claim detection: {result['unsupported_claim_detection_rate']}. "
        f"Reason-code precision: {result['reason_code_precision']:.4f}. "
        f"Citation failures detected: {result['citation_failures_detected']}. "
        f"Contradiction cases detected: {result['contradiction_cases_detected']}.",
        "",
        f"Repairs attempted: {result['repair_attempted']}; success rate "
        f"{result['repair_success_rate']}; cap of one respected: "
        f"{result['repair_cap_respected']}; drafts still unsupported after repair: "
        f"{result['post_repair_unsupported']}.",
        "",
        "A PASS here means every material claim survived every check on this fixture. It is not a "
        "claim about clinical correctness, and the cases were written alongside the checks they "
        "measure.",
        "",
        "Per-case reason codes and contradiction findings are in the paired JSON.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=ROOT / ".local/m8-verification.json")
    parser.add_argument("--md", type=Path, default=ROOT / ".local/m8-verification.md")
    args = parser.parse_args()

    report = {
        "notice": "Synthetic engineering benchmark. Not clinical validation.",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": commit(),
        "platform": platform.platform(),
        "dataset_sha256": hashlib.sha256((ROOT / GOLD).read_bytes()).hexdigest(),
        "policies": {
            "claim_extraction": ClaimExtractionConfig().model_dump(mode="json"),
            "claim_extraction_fingerprint": ClaimExtractionConfig().fingerprint,
            "claim_verification": ClaimVerificationConfig().model_dump(mode="json"),
            "claim_verification_fingerprint": ClaimVerificationConfig().fingerprint,
            "contradiction": ContradictionConfig().model_dump(mode="json"),
            "repair": RepairConfig().model_dump(mode="json"),
            "final_verification": FinalVerificationConfig().model_dump(mode="json"),
            "final_verification_fingerprint": FinalVerificationConfig().fingerprint,
        },
        "verification": evaluate(ROOT),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    args.md.write_text(markdown(report), encoding="utf-8")
    print(markdown(report))


if __name__ == "__main__":
    main()
