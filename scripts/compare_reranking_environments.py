"""Compare fixed-pair scores/ranks from the M6 host and canonical Linux reports."""

import argparse
import hashlib
import json
import struct
from pathlib import Path


def compare(host: dict, linux: dict) -> dict:
    if host["frozen_sha256"] != linux["frozen_sha256"]:
        raise ValueError("First-stage snapshots differ")
    for key in (
        "model_id",
        "model_revision",
        "tokenizer_revision",
        "files",
        "representation",
        "dtype",
        "device",
    ):
        if host["model"][key] != linux["model"][key]:
            raise ValueError("Model/input identity differs: " + key)
    results = {}
    for size, a in host["pools"].items():
        b = linux["pools"][size]
        queries = {q["case"]: q for q in b["per_query"]}
        equal = 0
        count = 0
        delta = 0.0
        top5 = 0
        complete = 0
        for query in a["per_query"]:
            other = queries[query["case"]]
            ar = query["ranks"]
            br = other["ranks"]
            scores = {r["chunk_id"]: r for r in br}
            for row in ar:
                remote = scores[row["chunk_id"]]
                assert row["input_hash"] == remote["input_hash"]
                assert row["token_count"] == remote["token_count"]
                count += 1
                equal += struct.pack("<f", row["score"]) == struct.pack("<f", remote["score"])
                delta = max(delta, abs(row["score"] - remote["score"]))
            top5 += [r["chunk_id"] for r in ar[:5]] == [r["chunk_id"] for r in br[:5]]
            complete += [r["chunk_id"] for r in ar] == [r["chunk_id"] for r in br]
        metrics_equal = all(
            a["reranked"][key] == b["reranked"][key]
            for key in ("recall", "precision", "mrr", "ndcg", "per_category")
        )
        results[size] = {
            "scores": count,
            "byte_identical_float32": equal,
            "max_absolute_delta": delta,
            "queries": len(queries),
            "top5_order_agreement": top5,
            "complete_order_agreement": complete,
            "ranking_metrics_equal": metrics_equal,
        }
    return {
        "frozen_sha256": host["frozen_sha256"],
        "model_revision": host["model"]["model_revision"],
        "host_libraries": host["model"]["library_versions"],
        "linux_libraries": linux["model"]["library_versions"],
        "pools": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = compare(
        json.loads(args.host.read_text(encoding="utf-8")),
        json.loads(args.linux.read_text(encoding="utf-8")),
    )
    report["input_files"] = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in (("host", args.host), ("linux", args.linux))
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
