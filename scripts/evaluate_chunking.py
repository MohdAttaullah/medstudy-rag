"""Run the chunk construction evaluation over the synthetic normalized gold corpus.

    uv run python scripts/evaluate_chunking.py

Fully offline: the fixtures are frozen normalized parse datasets, so no PDF is parsed and no
model weight is fetched. Only the bundled pinned MedCPT tokenizer is loaded, for measurement.

Exits non-zero when any expectation fails, so a chunker or policy change that silently drops
source text, splits a question from its options or destabilises chunk identity is visible.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.chunking_config import ChunkingConfig  # noqa: E402
from app.evaluation.chunking import evaluate, load_gold  # noqa: E402
from app.ingestion.chunking.tokenizer import LocalTokenizer  # noqa: E402

GOLD = ROOT / "backend/tests/fixtures/chunking/gold.json"


def main() -> int:
    config = ChunkingConfig()
    cases = load_gold(GOLD)
    tokens = LocalTokenizer(config)
    report = evaluate(cases, config, tokens)
    print(f"chunker: {config.chunker_name} {config.chunker_version}")
    print(f"policy: {config.version} ({config.fingerprint[:12]})")
    print(f"tokenizer: {config.tokenizer_name} @ {config.tokenizer_revision[:12]} (local, offline)")
    print(report.render())
    return 0 if not report.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
