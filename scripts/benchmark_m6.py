"""Measure the real M6 service stages against the configured development corpus.

Run inside the API container. Uses authenticated admin scope and the existing private inference
runtime; policies are request-local benchmark instances, not changes to the public API settings.
"""

import json
from pathlib import Path
from statistics import mean
from uuid import uuid4

from app.core.config import Settings
from app.core.reranking_config import RerankingConfig
from app.main import create_app
from app.services.evidence import EvidenceService


def main():
    settings = Settings()
    app = create_app(settings)
    control = app.state.control
    credential = next(c for c in settings.dev_principals if c.role == "admin")
    actor = control.auth.authenticate("Bearer " + credential.token.get_secret_value())
    report = {}
    for size in (10, 20, 40):
        config = settings.model_copy(update={"reranking": RerankingConfig(candidate_top_k=size)})
        service = EvidenceService(control.retrieval, config)
        service.search(actor, "Parameter Group Alpha Units", uuid4())
        runs = []
        for _ in range(5):
            result = service.search(actor, "Parameter Group Alpha Units", uuid4())
            evidence = result["evidence_set"]
            runs.append(
                {
                    "durations_ms": evidence["reranking_trace"]["durations_ms"],
                    "candidates": len(result["reranked"]),
                    "blocks": len(evidence["evidence_blocks"]),
                    "tokens": evidence["total_tokens"],
                }
            )
        keys = runs[0]["durations_ms"]
        report[str(size)] = {
            "runs": runs,
            "mean_ms": {key: mean(r["durations_ms"][key] for r in runs) for key in keys},
            "policy": config.reranking.model_dump(),
        }
    Path("/tmp/m6-service-benchmark.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps({size: result["mean_ms"] for size, result in report.items()}, indent=2))


if __name__ == "__main__":
    main()
