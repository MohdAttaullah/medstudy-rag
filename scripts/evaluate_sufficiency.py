"""Measure the M7 sufficiency gate and the grounded-draft contract. Never calls a provider."""

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

from app.core.generation_config import (  # noqa: E402
    GroundingConfig,
    ProviderConfig,
    SufficiencyConfig,
)
from app.evaluation.sufficiency import (  # noqa: E402
    GOLD,
    evaluate_generation,
    evaluate_sufficiency,
)


def commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def markdown(report: dict) -> str:
    gate = report["sufficiency"]
    generation = report["generation"]
    lines = [
        "# M7 sufficiency and grounded-draft evaluation",
        "",
        "Synthetic engineering benchmark over evidence built from the frozen M5 gold corpus. "
        "Anchors are prescribed, so this measures the gate rather than retrieval. "
        "No clinician annotations and no clinical accuracy claim.",
        "",
        f"Dataset `{gate['dataset_version']}`; policy `{gate['policy_version']}` "
        f"(`{gate['policy_fingerprint'][:12]}…`); {gate['cases_evaluated']} cases.",
        "",
        "| Outcome | Count |",
        "|---|---|",
    ]
    for key, value in gate["confusion"].items():
        lines.append(f"| {key.replace('->', ' → ')} | {value} |")
    lines += [
        "",
        f"Agreement with the labelled outcome: **{gate['agreement']:.4f}**. "
        f"Abstention rate: {gate['abstention_rate']:.4f}.",
        "",
        f"**False allows (generation permitted where the label forbids it): "
        f"{gate['false_allow_count']}** — {gate['false_allows'] or 'none'}.",
        "",
        f"Unnecessary abstentions: {gate['unnecessary_abstention_count']} — "
        f"{gate['unnecessary_abstentions'] or 'none'}. In this architecture an unnecessary "
        "abstention is the acceptable failure direction and a false allow is not; the two are "
        "reported separately for that reason.",
        "",
        "## Grounded draft contract",
        "",
        f"Generation attempted on {generation['generation_attempted']} of "
        f"{generation['cases_evaluated']} cases and suppressed on "
        f"{generation['generation_suppressed']}.",
        "",
        f"Schema validity {generation['schema_valid_rate']}; citation validity "
        f"{generation['citation_valid_rate']}; invented-citation rejection "
        f"{generation['invented_citation_rejection_rate']}; evidence-id hallucination rate "
        f"{generation['evidence_id_hallucination_rate']}.",
        "",
        f"Rank or score leaked into the provider's evidence rendering: "
        f"{generation['rank_or_score_leaked_to_provider']}.",
        "",
        "A grounded draft is **not** claim-verified. M7 checks that every citation names an "
        "evidence block that was actually supplied; whether the cited block supports the sentence "
        "is M8 claim verification and is not measured here.",
        "",
        "Per-case reason codes, evaluated signals and detected conflicts are in the paired JSON.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=ROOT / ".local/m7-sufficiency.json")
    parser.add_argument("--md", type=Path, default=ROOT / ".local/m7-sufficiency.md")
    args = parser.parse_args()

    policy = SufficiencyConfig()
    report = {
        "notice": "Synthetic engineering benchmark. Not clinical validation.",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": commit(),
        "platform": platform.platform(),
        "dataset_sha256": hashlib.sha256((ROOT / GOLD).read_bytes()).hexdigest(),
        "policies": {
            "sufficiency": policy.model_dump(mode="json"),
            "sufficiency_fingerprint": policy.fingerprint,
            "grounding": GroundingConfig().model_dump(mode="json"),
            "grounding_fingerprint": GroundingConfig().fingerprint,
            "provider": ProviderConfig().model_dump(mode="json"),
        },
        "sufficiency": evaluate_sufficiency(ROOT, policy),
        "generation": evaluate_generation(ROOT, policy),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    args.md.write_text(markdown(report), encoding="utf-8")
    print(markdown(report))


if __name__ == "__main__":
    main()
