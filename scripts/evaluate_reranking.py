"""Frozen M5 candidate pools, real CrossEncoder, and separate structural context gold.

First run --prepare to freeze first-stage pools using the same M5 encoders/configuration.
Subsequent comparisons consume the immutable pool artifact, isolating reranking arithmetic.
"""

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from statistics import mean
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from app.core.reranking_config import RerankerConfig  # noqa: E402
from app.core.retrieval_config import (  # noqa: E402
    QueryEncoderConfig,
    RetrievalConfig,
    SparseAnalyzerConfig,
)
from app.evaluation.retrieval import LaneScores, load_gold, recall_at  # noqa: E402
from app.reranking.medcpt import MedCPTReranker  # noqa: E402
from app.reranking.model import RerankInput  # noqa: E402
from app.retrieval.fusion.rrf import ReciprocalRankFusion  # noqa: E402
from app.retrieval.query.medcpt import MedCPTQueryEncoder  # noqa: E402
from app.retrieval.query.normalize import normalize  # noqa: E402
from app.retrieval.sparse.bm25 import InMemorySparseIndex  # noqa: E402
from evaluate_retrieval import dense_rank  # noqa: E402

GOLD = ROOT / "backend/tests/fixtures/retrieval/gold.json"
FROZEN = ROOT / "docs/evals/fixtures/m6-first-stage.json"


from app.evaluation.reranking import failures  # noqa: E402


def prepare(args, gold):
    from app.core.embedding_config import EmbeddingConfig
    from app.embeddings.inputs import ChunkSource, build
    from app.embeddings.medcpt import MedCPTArticleEmbedder

    ac = EmbeddingConfig(model_cache_dir=args.embedding_cache, offline=True)
    article = MedCPTArticleEmbedder(ac)
    inputs = [
        build(
            ChunkSource(
                chunk_id=c.chunk_id,
                chunk_type=c.chunk_type,
                retrieval_text=c.text,
                document_title=gold.document(c.document).title,
                hierarchy=c.hierarchy,
                caption=c.caption,
            ),
            ac,
        )
        for c in gold.chunks
    ]
    vectors = {
        v.chunk_id: v.values
        for start in range(0, len(inputs), ac.batch_size)
        for v in article.embed_documents(tuple(inputs[start : start + ac.batch_size]))
    }
    del article
    encoder = MedCPTQueryEncoder(
        QueryEncoderConfig(model_cache_dir=args.query_cache or args.embedding_cache, offline=True)
    )
    sparse = InMemorySparseIndex(config=SparseAnalyzerConfig())
    for chunk in gold.chunks:
        sparse.add(chunk.chunk_id, chunk.text)
    config = RetrievalConfig()
    records = []
    for case in gold.cases:
        if case.expect_error:
            continue
        encoded = encoder.encode_queries([case.query])[0]
        dense = dense_rank(encoded.values, vectors, config.dense_top_k)
        lexical = sparse.search(
            case.query, top_k=config.sparse_top_k, k1=config.bm25_k1, b=config.bm25_b
        )
        fused = ReciprocalRankFusion(config.rrf_k).fuse([dense, lexical], limit=40)
        records.append(
            {
                "case": case.id,
                "pool": [
                    {
                        "chunk_id": str(hit.chunk_id),
                        "fused_rank": hit.fused_rank,
                        "fused_score": hit.fused_score,
                    }
                    for hit in fused
                ],
            }
        )
    payload = {
        "gold_sha256": hashlib.sha256(GOLD.read_bytes()).hexdigest(),
        "baseline_commit": "ecb4941",
        "retrieval_config": config.model_dump(),
        "query_spec": str(encoder.specification),
        "records": records,
    }
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    FROZEN.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def markdown(report):
    lines = [
        "# M6 reranking evaluation",
        "",
        report["notice"],
        "",
        "| Pool | M5 MRR | M6 MRR | M5 nDCG@5 | M6 nDCG@5 | M6 P@5 | Pool recall | Inference mean ms |",  # noqa: E501
        "|---|---|---|---|---|---|---|---|",
    ]
    for pool, result in report["pools"].items():
        a, b = result["baseline"], result["reranked"]
        lines.append(
            f"| {pool} | {a['mrr']} | {b['mrr']} | {a['ndcg']['@5']} | {b['ndcg']['@5']} | {b['precision']['@5']} | {result['candidate_pool_recall']} | {result['inference_mean_ms']:.2f} |"  # noqa: E501
        )
    lines += [
        "",
        "Full per-query ranks, scores, relevant/irrelevant distributions, candidate recall, final recall, regressions, and category metrics are in the paired JSON.",  # noqa: E501
        "",
        f"Runtime: {report['platform']}; cold load {report['cold_ms']:.2f} ms.",
        "",
        "No sufficiency threshold or answer generation exists. Negative candidates are diagnostics, not established evidence.",  # noqa: E501
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--embedding-cache", type=Path, default=ROOT / ".local/models/embeddings")
    parser.add_argument("--query-cache", type=Path)
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/models/reranking")
    parser.add_argument("--json", type=Path, default=ROOT / ".local/m6-reranking-host.json")
    parser.add_argument("--md", type=Path, default=ROOT / ".local/m6-reranking-host.md")
    args = parser.parse_args()
    gold = load_gold(GOLD)
    if args.prepare:
        prepare(args, gold)
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    assert frozen["gold_sha256"] == hashlib.sha256(GOLD.read_bytes()).hexdigest()
    cases = {c.id: c for c in gold.cases}
    chunks = {c.chunk_id: c for c in gold.chunks}
    reranker = MedCPTReranker(RerankerConfig(model_cache_dir=args.cache))
    start = time.perf_counter()
    spec = reranker.specification
    cold = (time.perf_counter() - start) * 1000
    report = {
        "notice": "Synthetic software evaluation: 83 chunks, 22 positive queries, two negatives. One M5 query-length boundary remains rejected before reranking. No clinician annotations or clinical accuracy claim.",  # noqa: E501
        "platform": platform.platform(),
        "model": spec.model_dump(mode="json"),
        "cold_ms": cold,
        "frozen_sha256": hashlib.sha256(FROZEN.read_bytes()).hexdigest(),
        "pools": {},
    }
    # One warm-up, excluded from measurements; same model instance throughout all pool sizes.
    first = next(iter(chunks.values()))
    reranker.rerank("warmup", [RerankInput(chunk_id=first.chunk_id, text=first.text, fused_rank=1)])
    for size in (10, 20, 30, 40):
        baseline = LaneScores(lane="HYBRID_RRF")
        ranked = LaneScores(lane="HYBRID_RRF+CROSS_ENCODER")
        diagnostics = []
        latencies = []
        recalls = []
        relevant_scores = []
        irrelevant_scores = []
        negative_scores = []
        for record in frozen["records"]:
            case = cases[record["case"]]
            pool = record["pool"][:size]
            ids = [UUID(h["chunk_id"]) for h in pool]
            inputs = [
                RerankInput(chunk_id=identifier, text=chunks[identifier].text, fused_rank=i)
                for i, identifier in enumerate(ids, 1)
            ]
            start = time.perf_counter()
            scores = reranker.rerank(normalize(case.query, QueryEncoderConfig()), inputs)
            elapsed = (time.perf_counter() - start) * 1000
            order = [r.chunk_id for r in scores]
            latencies.append(elapsed)
            baseline.observe(case, ids, 0, pool[0]["fused_score"] if pool else None)
            ranked.observe(case, order, elapsed, scores[0].reranker_score if scores else None)
            if not case.negative:
                recalls.append(recall_at(ids, case.relevant_ids, size))
            for result in scores:
                (
                    negative_scores
                    if case.negative
                    else relevant_scores
                    if result.chunk_id in case.relevant_ids
                    else irrelevant_scores
                ).append(result.reranker_score)
            diagnostics.append(
                {
                    "case": case.id,
                    "category": case.category,
                    "negative": case.negative,
                    "candidate_count": len(ids),
                    "candidate_pool_recall": None
                    if case.negative
                    else recall_at(ids, case.relevant_ids, size),
                    "final_recall_at_5": None
                    if case.negative
                    else recall_at(order, case.relevant_ids, 5),
                    "failure_classes": failures(case.relevant_ids, ids, ids, order),
                    "inference_ms": elapsed,
                    "ranks": [
                        {
                            "chunk_id": str(r.chunk_id),
                            "label": chunks[r.chunk_id].label,
                            "m5_rank": ids.index(r.chunk_id) + 1,
                            "reranked_rank": r.reranked_rank,
                            "rank_delta": ids.index(r.chunk_id) + 1 - r.reranked_rank,
                            "score": r.reranker_score,
                            "grade": case.grade(r.chunk_id),
                            "selected_anchor": r.reranked_rank <= 5,
                            "input_hash": r.input_hash,
                            "token_count": r.token_count,
                        }
                        for r in scores
                    ],
                }
            )
        report["pools"][str(size)] = {
            "baseline": baseline.summary(),
            "reranked": ranked.summary(),
            "candidate_pool_recall": mean(recalls),
            "inference_mean_ms": mean(latencies),
            "scores": {
                "relevant": relevant_scores,
                "irrelevant": irrelevant_scores,
                "negative": negative_scores,
            },
            "per_query": diagnostics,
        }
    from app.evaluation.evidence import evaluate_context

    report["context"] = evaluate_context(ROOT)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    args.md.write_text(markdown(report), encoding="utf-8")
    print(markdown(report))


if __name__ == "__main__":
    main()
