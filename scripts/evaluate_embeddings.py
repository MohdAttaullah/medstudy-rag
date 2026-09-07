"""Run the embedding technical-quality evaluation over the synthetic gold corpus.

    uv run --extra embedding python scripts/evaluate_embeddings.py [--cache DIR]

Offline: the pinned model must already be in the local cache (see
scripts/provision_embedding_model.py). Nothing is downloaded, no index is touched and no query is
issued. Exits non-zero when any expectation fails, so a model, revision or input-builder change
that silently alters what a vector means is visible instead of absorbed.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.embedding_config import EmbeddingConfig  # noqa: E402
from app.embeddings.medcpt import MedCPTArticleEmbedder  # noqa: E402
from app.evaluation.embeddings import evaluate, load_gold  # noqa: E402

GOLD = ROOT / "backend/tests/fixtures/embedding/gold.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/models/embeddings")
    parser.add_argument("--json", type=Path, help="Write a machine-readable report here.")
    arguments = parser.parse_args()

    config = EmbeddingConfig(model_cache_dir=arguments.cache, offline=True, batch_size=4)
    cases, similarity = load_gold(GOLD)
    model = MedCPTArticleEmbedder(config)
    report = evaluate(cases, similarity, model, config)
    if arguments.json:
        arguments.json.parent.mkdir(parents=True, exist_ok=True)
        arguments.json.write_text(
            json.dumps(
                {
                    "dataset_version": "embedding-gold-m4-v1",
                    "policy_version": config.version,
                    "policy_fingerprint": config.fingerprint,
                    "vector_space_fingerprint": config.semantics_fingerprint,
                    "dimension": config.embedding_dimension,
                    "pooling": config.pooling_strategy,
                    "distance": config.distance_metric,
                    "cases_evaluated": report.cases,
                    "passed_cases": report.passed_cases,
                    "checks": report.checks,
                    "failure_count": len(report.failures),
                    "failures": list(report.failures),
                    "specification": report.specification,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    print(f"policy: {config.version} ({config.fingerprint[:12]})")
    print(f"vector space: {config.semantics_fingerprint[:12]}")
    print(report.render())
    return 0 if not report.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
