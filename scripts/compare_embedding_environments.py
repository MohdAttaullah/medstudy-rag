"""Measure whether two environments produce the same vectors for the same input.

    # on the host
    uv run --extra embedding python scripts/compare_embedding_environments.py --emit host.json
    # in the canonical Linux worker image
    docker run --rm -v <repo>:/repo ... python /repo/scripts/compare_embedding_environments.py \
        --emit /repo/worker.json --cache /home/medrag/models/embeddings
    # then
    uv run python scripts/compare_embedding_environments.py --compare host.json worker.json

Reports byte identity where it holds, and the actual numerical deviation and similarity-ordering
agreement where it does not. It does not assert cross-device bit identity, because PyTorch and the
underlying BLAS do not guarantee it; the point of this script is to measure the difference rather
than to assume it away.

The canonical stored corpus embeddings are the ones produced by the Linux worker.
"""

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.embedding_config import EmbeddingConfig  # noqa: E402
from app.embeddings.inputs import build  # noqa: E402
from app.embeddings.medcpt import MedCPTArticleEmbedder  # noqa: E402
from app.evaluation.embeddings import load_gold  # noqa: E402

GOLD = ROOT / "backend/tests/fixtures/embedding/gold.json"


def emit(destination: Path, cache: Path) -> int:
    config = EmbeddingConfig(model_cache_dir=cache, offline=True, batch_size=4)
    cases, _ = load_gold(GOLD)
    usable = [case for case in cases if case.expect_eligible and not case.expect_over_limit]
    model = MedCPTArticleEmbedder(config)
    specification = model.specification
    inputs = tuple(build(case.source(), config) for case in usable)
    vectors = {vector.chunk_id: vector for vector in model.embed_documents(inputs)}
    payload = {
        "model_revision": specification.model_revision,
        "model_checksum": specification.model_checksum,
        "tokenizer_revision": specification.tokenizer_revision,
        "libraries": specification.library_versions,
        "semantics_fingerprint": config.semantics_fingerprint,
        "vectors": {
            case.id: {
                "input_hash": value.input_hash,
                "checksum": vectors[case.chunk_id].checksum,
                "norm": vectors[case.chunk_id].norm,
                "values": list(vectors[case.chunk_id].values),
            }
            for case, value in zip(usable, inputs, strict=True)
        },
    }
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(payload['vectors'])} vectors to {destination}")
    return 0


def compare(first: Path, second: Path) -> int:
    left = json.loads(first.read_text(encoding="utf-8"))
    right = json.loads(second.read_text(encoding="utf-8"))
    print(f"A: {first}\nB: {second}\n")
    for field in (
        "model_revision",
        "model_checksum",
        "tokenizer_revision",
        "semantics_fingerprint",
    ):
        same = left[field] == right[field]
        print(f"{field:22s} {'identical' if same else 'DIFFERENT'}  {left[field][:24]}")
    print(f"libraries A            {left['libraries']}")
    print(f"libraries B            {right['libraries']}")

    shared = sorted(set(left["vectors"]) & set(right["vectors"]))
    identical = 0
    worst_delta = 0.0
    worst_cosine = 1.0
    for name in shared:
        a, b = left["vectors"][name], right["vectors"][name]
        if a["input_hash"] != b["input_hash"]:
            print(f"  {name}: INPUT HASH DIFFERS - the two runs did not embed the same text")
            continue
        if a["checksum"] == b["checksum"]:
            identical += 1
            continue
        delta = max(abs(x - y) for x, y in zip(a["values"], b["values"], strict=True))
        dot = sum(x * y for x, y in zip(a["values"], b["values"], strict=True))
        norms = math.sqrt(sum(x * x for x in a["values"])) * math.sqrt(
            sum(y * y for y in b["values"])
        )
        cosine = dot / norms if norms else 0.0
        worst_delta = max(worst_delta, delta)
        worst_cosine = min(worst_cosine, cosine)
        print(f"  {name}: max|delta| {delta:.3e}  cosine {cosine:.9f}")

    print(f"\nvectors compared:      {len(shared)}")
    print(f"byte identical:        {identical}/{len(shared)}")
    if identical < len(shared):
        print(f"worst max|delta|:      {worst_delta:.3e}")
        print(f"worst cosine:          {worst_cosine:.9f}")

    # Ranking agreement: do the two environments order the same pairs the same way?
    names = shared
    disagreements = 0
    comparisons = 0

    def score(source: dict, anchor: str, other: str) -> float:
        return sum(
            x * y
            for x, y in zip(
                source["vectors"][anchor]["values"],
                source["vectors"][other]["values"],
                strict=True,
            )
        )

    for anchor in names:
        others = [name for name in names if name != anchor]
        ranked_left = sorted(others, key=lambda name, a=anchor: -score(left, a, name))
        ranked_right = sorted(others, key=lambda name, a=anchor: -score(right, a, name))
        comparisons += 1
        if ranked_left != ranked_right:
            disagreements += 1
    print(
        f"ranking agreement:     {comparisons - disagreements}/{comparisons} anchors order "
        f"every other fixture identically"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit", type=Path)
    parser.add_argument("--compare", type=Path, nargs=2)
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/models/embeddings")
    arguments = parser.parse_args()
    if arguments.emit:
        return emit(arguments.emit, arguments.cache)
    if arguments.compare:
        return compare(*arguments.compare)
    parser.error("pass --emit PATH or --compare A B")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
