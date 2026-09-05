"""Run the parsing extraction-fidelity evaluation over the synthetic gold corpus.

    uv run --extra parsing python scripts/evaluate_parsing.py

Exits non-zero when any expectation fails, so a parser or configuration change that silently
degrades structural extraction is visible rather than absorbed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.parsing_config import ParsingConfig  # noqa: E402
from app.evaluation.parsing import evaluate, load_gold  # noqa: E402
from app.ingestion.parser.docling_adapter import DoclingDocumentParser  # noqa: E402
from app.ingestion.parser.model import ParserConfig, ParseSource  # noqa: E402

GOLD = ROOT / "docs/evals/parsing-gold.json"


def main() -> int:
    cases = load_gold(GOLD)
    policy = ParsingConfig()
    corpus = ROOT / "backend/tests/fixtures/parsing"
    parser = DoclingDocumentParser()
    config = ParserConfig(
        ocr_mode=policy.ocr_mode.value,
        extract_tables=policy.extract_tables,
        extract_formulas=policy.extract_formulas,
        extract_figures=policy.extract_figures,
        generate_page_previews=False,  # previews are irrelevant to extraction fidelity
        preview_scale=policy.preview_scale,
        timeout_seconds=policy.timeout_seconds,
        max_pages=policy.max_pages,
        threads=policy.parser_threads,
    )

    def parse(document: str):
        path = corpus / document
        return parser.parse(ParseSource(path=path, sha256="", filename=document), config)

    report = evaluate(cases, parse)
    print(f"parser: docling {parser.version}")
    print(f"policy: {policy.version} ({policy.fingerprint[:12]})")
    print(report.render())
    return 0 if not report.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
