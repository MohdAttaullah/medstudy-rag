"""The single M11 evaluation entry point.

    uv run python scripts/evaluate_m11.py                     # offline, no API key, no models
    uv run --extra embedding python scripts/evaluate_m11.py --include-models
    uv run --extra embedding python scripts/evaluate_m11.py --live   # opt-in provider calls

Offline is the default and is what CI runs: it calls no provider, needs no API key, and touches
neither PostgreSQL nor Qdrant. Every hard safety invariant is measured in that mode, because the
things this architecture must never do are decided by code the harness can exercise directly.

`--live` sends real requests to the configured provider and costs money. It is never implied by
another flag, and it refuses to run without an explicitly configured generator. What it produces
is evidence that the integration works, over a handful of synthetic questions. It is not evidence
about the model, and it is not clinical validation.

Exits non-zero when a quality gate fails or a hard safety invariant went unmeasured.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The report is UTF-8; a Windows console defaulting to cp1252 would otherwise fail on the first
# typographic character rather than on anything to do with the evaluation.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import Settings  # noqa: E402
from app.evaluation import harness  # noqa: E402
from app.evaluation.report import render  # noqa: E402

DEFAULT_OUT = "docs/evals/m11"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _per_case_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One reviewable row per case, across every layer that produced cases.

    Shaped for a future medical reviewer: question, expectation, what the system did, the
    evidence it used and why it refused. It deliberately carries no provider reasoning and no
    chain of thought — a reviewer needs the decision and its evidence, not the model's monologue.
    """
    rows = []
    for layer, payload in report.get("layers", {}).items():
        if not isinstance(payload, dict):
            continue
        # Some layers report `cases` as a count rather than a list; only per-case detail is rows.
        detail = payload.get("cases")
        if not isinstance(detail, list):
            continue
        for case in detail:
            if not isinstance(case, dict):
                continue
            rows.append(
                {
                    "layer": layer,
                    "dataset_id": payload.get("dataset_id"),
                    "dataset_version": payload.get("dataset_version"),
                    "independence": payload.get("independence"),
                    **{k: v for k, v in case.items() if k not in {"signals", "conflicts"}},
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the layered M11 evaluation.")
    parser.add_argument(
        "--include-models",
        action="store_true",
        help="Also run parsing, embedding, retrieval and reranking (needs local model weights).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Additionally run the opt-in live provider evaluation. Costs money.",
    )
    parser.add_argument("--only", help="Comma-separated layer names to run.")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"Artifact directory ({DEFAULT_OUT}).")
    parser.add_argument(
        "--allow-gate-failure",
        action="store_true",
        help="Report gate failures without exiting non-zero. For investigation only.",
    )
    args = parser.parse_args()

    settings = Settings(_env_file=str(ROOT / ".env") if (ROOT / ".env").exists() else None)
    only = frozenset(p.strip() for p in args.only.split(",")) if args.only else None

    report = harness.run(ROOT, settings, include_models=args.include_models, only=only)

    if args.live:
        from app.evaluation.live import run_live

        report["layers"]["live"] = run_live(ROOT, settings)
        report["layer_status"]["live"] = {"status": "RUN"}
        report["run"]["mode"] = "live"
        # Gates are recomputed so a live run is judged on what it actually measured.
        from app.evaluation import gates

        report["quality_gates"] = gates.evaluate_all(report)

    out = ROOT / args.out
    _write(out / "m11-report.json", json.dumps(report, indent=2, default=str))
    _write(
        out / "m11-cases.jsonl",
        "\n".join(json.dumps(row, default=str) for row in _per_case_rows(report)) + "\n",
    )
    _write(out / "m11-report.md", render(report))

    verdict = report["quality_gates"]
    print(render(report))
    print(f"\nArtifacts written to {out.relative_to(ROOT)}/")

    if not verdict["passed"] and not args.allow_gate_failure:
        failed = ", ".join(verdict["failed"]) or "a safety invariant was not measured"
        print(f"\nFAIL: {failed}", file=sys.stderr)
        raise SystemExit(1)
    print("\nPASS: no quality gate failed and every hard safety invariant was measured and upheld.")


if __name__ == "__main__":
    main()
