"""Measure first-stage retrieval quality against the gold query set.

    uv run --extra embedding python scripts/evaluate_retrieval.py [--sweep] [--json OUT] [--md OUT]

Runs the same analyzer, the same BM25 scorer, the same RRF implementation and the same two MedCPT
encoders the service uses, over an in-memory index built from the synthetic gold corpus. What it
deliberately does not exercise is PostgreSQL, Qdrant, tenancy and authorization; those are proven
by the integration tests, and keeping them out of the harness is what lets an evaluation run on a
laptop with no stack up.

It never calls a language model, and it produces no answers — only Recall@K, MRR, nDCG, precision
diagnostics, per-category breakdowns, failure analysis and latency.

Every number it prints is a measurement over a small synthetic corpus written for this repository.
None of it is evidence about clinical performance.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.embedding_config import EmbeddingConfig  # noqa: E402
from app.core.retrieval_config import (  # noqa: E402
    QueryEncoderConfig,
    RetrievalConfig,
    SparseAnalyzerConfig,
)
from app.embeddings.inputs import ChunkSource, build  # noqa: E402
from app.embeddings.medcpt import MedCPTArticleEmbedder  # noqa: E402
from app.evaluation.retrieval import (  # noqa: E402
    GoldSet,
    LaneScores,
    classify,
    load_gold,
)
from app.retrieval.errors import RetrievalError  # noqa: E402
from app.retrieval.fusion.rrf import ReciprocalRankFusion  # noqa: E402
from app.retrieval.model import LaneHit, Ranking  # noqa: E402
from app.retrieval.query.medcpt import MedCPTQueryEncoder  # noqa: E402
from app.retrieval.sparse.bm25 import InMemorySparseIndex  # noqa: E402

GOLD = ROOT / "backend/tests/fixtures/retrieval/gold.json"


def _digest(path: Path) -> str:
    """SHA-256 of the gold file, so a metric is tied to the exact bytes it was measured over."""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - a report outside a checkout still records "unknown"
        return "unknown"


def dense_rank(
    query_vector: tuple[float, ...],
    corpus: dict[UUID, tuple[float, ...]],
    top_k: int,
) -> Ranking:
    """Exhaustive inner product over the fixture corpus.

    Exact rather than approximate on purpose: the harness measures the *representation*, and an
    approximate index would fold its own recall loss into every number without saying so.
    """
    started = time.perf_counter()
    scored = sorted(
        (
            (chunk_id, sum(a * b for a, b in zip(query_vector, values, strict=True)))
            for chunk_id, values in corpus.items()
        ),
        key=lambda item: (-item[1], str(item[0])),
    )[:top_k]
    return Ranking(
        lane="DENSE",
        hits=tuple(
            LaneHit(chunk_id=chunk_id, score=score, rank=position)
            for position, (chunk_id, score) in enumerate(scored, start=1)
        ),
        duration_ms=(time.perf_counter() - started) * 1000,
        scanned=len(corpus),
    )


def evaluate(
    gold: GoldSet,
    vectors: dict[UUID, tuple[float, ...]],
    sparse: InMemorySparseIndex,
    encoder: MedCPTQueryEncoder,
    config: RetrievalConfig,
) -> dict[str, Any]:
    lanes = {name: LaneScores(lane=name) for name in ("DENSE", "BM25", "HYBRID_RRF")}
    fusion = ReciprocalRankFusion(config.rrf_k)
    corpus_ids = set(vectors)
    diagnostics: list[dict[str, Any]] = []
    boundary: list[dict[str, Any]] = []
    per_query: list[dict[str, Any]] = []

    for case in gold.cases:
        started = time.perf_counter()
        try:
            encoded = encoder.encode_queries([case.query])
        except RetrievalError as exc:
            # The encoder's token limit bounds the dense and hybrid lanes only. BM25 never sees
            # the encoder, so a question too long to encode is still perfectly searchable
            # lexically — and reporting that lane as unavailable would misdescribe the system.
            lexical = sparse.search(
                case.query, top_k=config.sparse_top_k, k1=config.bm25_k1, b=config.bm25_b
            )
            for lane in lanes.values():
                lane.errors.append(
                    {
                        "case": case.id,
                        "code": exc.code,
                        "expected": exc.code == case.expect_error,
                    }
                )
            boundary.append(
                {
                    "case": case.id,
                    "category": case.category,
                    "encoder_code": exc.code,
                    "expected_code": case.expect_error,
                    "as_expected": exc.code == case.expect_error,
                    "encoder_token_limit": encoder.config.max_query_tokens,
                    "observed_tokens": exc.details.get("observed"),
                    "dense_available": False,
                    "hybrid_available": False,
                    "sparse_available": True,
                    "sparse_candidates": len(lexical.hits),
                    "sparse_query_terms": len(_terms(case.query, sparse.config)),
                    "note": (
                        "The query exceeds the query encoder's token limit and was rejected, "
                        "never truncated. BM25 does not use that limit and still ranks."
                    ),
                }
            )
            continue
        if case.expect_error:
            # The encoder accepted a query the dataset expected it to refuse. That is a change in
            # boundary behaviour and is recorded as such rather than quietly ignored.
            for lane in lanes.values():
                lane.errors.append(
                    {"case": case.id, "code": None, "expected_code": case.expect_error}
                )
            boundary.append(
                {
                    "case": case.id,
                    "category": case.category,
                    "encoder_code": None,
                    "expected_code": case.expect_error,
                    "as_expected": False,
                    "encoder_token_limit": encoder.config.max_query_tokens,
                    "observed_tokens": encoded[0].token_count,
                    "dense_available": True,
                    "hybrid_available": True,
                    "sparse_available": True,
                    "note": "The dataset expected a rejection and the encoder accepted the query.",
                }
            )
            continue

        encoding_ms = (time.perf_counter() - started) * 1000
        dense = dense_rank(encoded[0].values, vectors, config.dense_top_k)
        lexical = sparse.search(
            case.query,
            top_k=config.sparse_top_k,
            k1=config.bm25_k1,
            b=config.bm25_b,
        )
        fused_started = time.perf_counter()
        fused = fusion.fuse(
            [
                Ranking(
                    lane="DENSE",
                    hits=dense.hits,
                    duration_ms=dense.duration_ms,
                    weight=config.dense_weight,
                ),
                Ranking(
                    lane="BM25",
                    hits=lexical.hits,
                    duration_ms=lexical.duration_ms,
                    weight=config.sparse_weight,
                ),
            ],
            config.final_top_k,
        )
        fusion_ms = (time.perf_counter() - fused_started) * 1000

        dense_ids = [hit.chunk_id for hit in dense.hits]
        sparse_ids = [hit.chunk_id for hit in lexical.hits]
        fused_ids = [hit.chunk_id for hit in fused]
        lanes["DENSE"].observe(
            case,
            dense_ids,
            encoding_ms + dense.duration_ms,
            dense.hits[0].score if dense.hits else None,
        )
        lanes["BM25"].observe(
            case, sparse_ids, lexical.duration_ms, lexical.hits[0].score if lexical.hits else None
        )
        lanes["HYBRID_RRF"].observe(
            case,
            fused_ids,
            encoding_ms + dense.duration_ms + lexical.duration_ms + fusion_ms,
            fused[0].fused_score if fused else None,
        )
        # Recorded for every case, not only failures: a case that succeeded at rank 1 in one lane
        # and rank 9 in another is exactly the evidence a later reranking milestone will need,
        # and it is invisible in an aggregate.
        terms = _terms(case.query, sparse.config)
        per_query.append(
            {
                "case": case.id,
                "category": case.category,
                "negative": case.negative,
                "graded": case.graded,
                "expected": sorted(case.relevant),
                "query_tokens": encoded[0].token_count,
                "query_terms": len(terms),
                "dense_rank_of_expected": _positions(case, dense_ids),
                "sparse_rank_of_expected": _positions(case, sparse_ids),
                "fused_rank_of_expected": _positions(case, fused_ids),
                "dense_top_score": dense.hits[0].score if dense.hits else None,
                "sparse_top_score": lexical.hits[0].score if lexical.hits else None,
                "fused_top_score": fused[0].fused_score if fused else None,
                "dense_candidates": len(dense.hits),
                "sparse_candidates": len(lexical.hits),
                "fused_candidates": len(fused),
                "failure_class": (
                    None
                    if case.negative or (case.relevant_ids & set(fused_ids))
                    else classify(case, dense_ids, sparse_ids, fused_ids, corpus_ids)
                ),
            }
        )
        if not case.negative and not (case.relevant_ids & set(fused_ids)):
            diagnostics.append(
                {
                    "case": case.id,
                    "category": case.category,
                    "failure_class": classify(case, dense_ids, sparse_ids, fused_ids, corpus_ids),
                    "expected": sorted(case.relevant),
                    "dense_rank_of_expected": _positions(case, dense_ids),
                    "sparse_rank_of_expected": _positions(case, sparse_ids),
                    "fused_rank_of_expected": _positions(case, fused_ids),
                    "query_terms": list(terms),
                    "notes": case.notes,
                }
            )

    return {
        "lanes": {name: lane.summary() for name, lane in lanes.items()},
        "failure_analysis": diagnostics,
        "query_boundary": boundary,
        "per_query": per_query,
    }


def _terms(query: str, analyzer: SparseAnalyzerConfig) -> tuple[str, ...]:
    from app.retrieval.sparse.analyzer import query_terms

    return query_terms(query, analyzer)


def _positions(case: Any, ranked: list[UUID]) -> dict[str, int | None]:
    relevant = {label: None for label in case.relevant}
    from app.evaluation.retrieval import fixture_id

    for label in case.relevant:
        target = fixture_id(label)
        relevant[label] = next(
            (position for position, chunk_id in enumerate(ranked, start=1) if chunk_id == target),
            None,
        )
    return relevant


def markdown(report: dict[str, Any]) -> str:
    lanes = report["lanes"]
    lines = [
        "# Retrieval evaluation",
        "",
        f"Dataset `{report['dataset_version']}` — {report['scored_cases']} scored cases, "
        f"{report['negative_cases']} negative, {report['corpus_chunks']} corpus chunks.",
        "",
        "**Synthetic corpus.** These are measurements of retrieval mechanics over text written "
        "for this repository. They are not evidence about clinical performance.",
        "",
        "| Lane | Recall@1 | Recall@5 | Recall@10 | Recall@20 | MRR | nDCG@5 | nDCG@10 | P@5 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name in ("DENSE", "BM25", "HYBRID_RRF"):
        lane = lanes[name]
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                name,
                _cell(lane["recall"].get("@1")),
                _cell(lane["recall"].get("@5")),
                _cell(lane["recall"].get("@10")),
                _cell(lane["recall"].get("@20")),
                _cell(lane["mrr"]),
                _cell(lane["ndcg"].get("@5")),
                _cell(lane["ndcg"].get("@10")),
                _cell(lane["precision"].get("@5")),
            )
        )
    categories = sorted({category for lane in lanes.values() for category in lane["per_category"]})
    lines += [
        "",
        "## Recall@5 by category",
        "",
        "| Category | Dense | BM25 | Hybrid RRF |",
        "|---|---|---|---|",
    ]
    for category in categories:
        lines.append(
            "| {} | {} | {} | {} |".format(
                category,
                _cell(lanes["DENSE"]["per_category"].get(category, {}).get("recall@5")),
                _cell(lanes["BM25"]["per_category"].get(category, {}).get("recall@5")),
                _cell(lanes["HYBRID_RRF"]["per_category"].get(category, {}).get("recall@5")),
            )
        )
    if report["failure_analysis"]:
        lines += [
            "",
            "## Failed cases (hybrid)",
            "",
            "| Case | Category | Class | Dense | BM25 |",
            "|---|---|---|---|---|",
        ]
        for item in report["failure_analysis"]:
            lines.append(
                "| {} | {} | {} | {} | {} |".format(
                    item["case"],
                    item["category"],
                    item["failure_class"],
                    item["dense_rank_of_expected"],
                    item["sparse_rank_of_expected"],
                )
            )
    else:
        lines += ["", "No gold case failed to retrieve its evidence within the fused budget."]
    lines += [
        "",
        "## Latency",
        "",
        "| Lane | p50 ms | p95 ms |",
        "|---|---|---|",
    ]
    for name in ("DENSE", "BM25", "HYBRID_RRF"):
        latency = lanes[name]["latency_ms"]
        lines.append(f"| {name} | {_cell(latency['p50'])} | {_cell(latency['p95'])} |")
    return "\n".join(lines) + "\n"


def _cell(value: Any) -> str:
    return "—" if value is None else f"{value}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=GOLD)
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/models/embeddings")
    parser.add_argument("--query-cache", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Also measure candidate budgets and RRF constants instead of assuming the defaults.",
    )
    arguments = parser.parse_args()

    gold = load_gold(arguments.gold)
    analyzer = SparseAnalyzerConfig()
    article_config = EmbeddingConfig(model_cache_dir=arguments.cache, offline=True)
    query_config = QueryEncoderConfig(
        model_cache_dir=arguments.query_cache or arguments.cache, offline=True
    )

    print(f"gold dataset:   {gold.version}")
    print(f"corpus chunks:  {len(gold.chunks)}")
    print(f"cases:          {len(gold.cases)}")
    print("loading encoders ...")
    article = MedCPTArticleEmbedder(article_config)
    encoder = MedCPTQueryEncoder(query_config)
    load_started = time.perf_counter()
    article.load()
    encoder.load()
    load_seconds = time.perf_counter() - load_started

    sparse = InMemorySparseIndex(config=analyzer)
    inputs = []
    for chunk in gold.chunks:
        document = gold.document(chunk.document)
        sparse.add(
            chunk.chunk_id,
            chunk.text,
            document_id=None,
            chunk_type=chunk.chunk_type,
            source_type=document.source_type,
            authority_level=document.authority_level,
        )
        inputs.append(
            build(
                ChunkSource(
                    chunk_id=chunk.chunk_id,
                    chunk_type=chunk.chunk_type,
                    retrieval_text=chunk.text,
                    document_title=document.title,
                    hierarchy=chunk.hierarchy,
                    caption=chunk.caption,
                ),
                article_config,
            )
        )
    embed_started = time.perf_counter()
    vectors = {
        vector.chunk_id: vector.values
        for start in range(0, len(inputs), article_config.batch_size)
        for vector in article.embed_documents(
            tuple(inputs[start : start + article_config.batch_size])
        )
    }
    embed_seconds = time.perf_counter() - embed_started

    config = RetrievalConfig()
    report = evaluate(gold, vectors, sparse, encoder, config)
    report.update(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "git_commit": commit(),
            "dataset_version": gold.version,
            # The bytes of the dataset, so a reported number can be tied to the exact corpus and
            # judgments that produced it rather than to a version string somebody could edit.
            "dataset_sha256": _digest(arguments.gold),
            "dataset_path": str(arguments.gold),
            "dataset_notice": gold.notice,
            # This harness runs offline against the fixture corpus, so there is no ChunkRun,
            # EmbeddingRun, IndexRun or SparseIndex to name. Said explicitly, because an absent
            # identity field should never be read as an unrecorded one. The end-to-end
            # integration test and scripts/smoke_m5.py exercise the durable identities.
            "corpus_identity": {
                "source": "offline gold fixture",
                "chunk_run_id": None,
                "embedding_run_id": None,
                "index_run_id": None,
                "sparse_index_id": None,
                "note": (
                    "Durable corpus identities exist only in the database-backed pipeline; this "
                    "harness measures the retrieval algorithms over a fixed fixture instead."
                ),
            },
            "corpus_chunks": len(gold.chunks),
            "scored_cases": len(
                [case for case in gold.cases if not case.negative and not case.expect_error]
            ),
            "negative_cases": len(
                [case for case in gold.cases if case.negative and not case.expect_error]
            ),
            "article_encoder": {
                "model_id": article_config.model_id,
                "revision": article_config.model_revision,
                "semantics_fingerprint": article_config.semantics_fingerprint,
            },
            "query_encoder": {
                "model_id": query_config.model_id,
                "revision": query_config.model_revision,
                "semantics_fingerprint": query_config.semantics_fingerprint,
                "library_versions": encoder.specification.library_versions,
            },
            "analyzer": {
                "version": analyzer.analyzer_version,
                "fingerprint": analyzer.analyzer_fingerprint,
                "case_policy": analyzer.case_policy,
                "compound_policy": analyzer.compound_policy,
                "stopwords": len(analyzer.stopwords),
            },
            "retrieval_config": config.model_dump(mode="json"),
            "retrieval_config_fingerprint": config.fingerprint,
            "timings_seconds": {
                "encoder_load": round(load_seconds, 2),
                "corpus_embedding": round(embed_seconds, 2),
            },
            "vocabulary_terms": len(sparse.document_frequency),
            "postings": len(sparse.postings),
        }
    )

    if arguments.sweep:
        report["sweeps"] = _sweep(gold, vectors, sparse, encoder)

    print()
    print(markdown(report))
    if arguments.json:
        arguments.json.parent.mkdir(parents=True, exist_ok=True)
        arguments.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", "utf-8")
        print(f"wrote {arguments.json}")
    if arguments.md:
        arguments.md.parent.mkdir(parents=True, exist_ok=True)
        arguments.md.write_text(markdown(report), encoding="utf-8")
        print(f"wrote {arguments.md}")
    return 0


def _sweep(
    gold: GoldSet,
    vectors: dict[UUID, tuple[float, ...]],
    sparse: InMemorySparseIndex,
    encoder: MedCPTQueryEncoder,
) -> dict[str, Any]:
    """Measure the parameters M5 refuses to assume: candidate budgets and the RRF constant."""
    budgets = []
    for top_k in (10, 20, 40, 60):
        config = RetrievalConfig(dense_top_k=top_k, sparse_top_k=top_k)
        outcome = evaluate(gold, vectors, sparse, encoder, config)
        budgets.append(
            {
                "top_k": top_k,
                "dense_recall@10": outcome["lanes"]["DENSE"]["recall"].get("@10"),
                "bm25_recall@10": outcome["lanes"]["BM25"]["recall"].get("@10"),
                "hybrid_recall@10": outcome["lanes"]["HYBRID_RRF"]["recall"].get("@10"),
                "hybrid_mrr": outcome["lanes"]["HYBRID_RRF"]["mrr"],
            }
        )
    constants = []
    for rrf_k in (10, 20, 60, 100):
        config = RetrievalConfig(rrf_k=rrf_k)
        outcome = evaluate(gold, vectors, sparse, encoder, config)
        constants.append(
            {
                "rrf_k": rrf_k,
                "hybrid_recall@5": outcome["lanes"]["HYBRID_RRF"]["recall"].get("@5"),
                "hybrid_recall@10": outcome["lanes"]["HYBRID_RRF"]["recall"].get("@10"),
                "hybrid_mrr": outcome["lanes"]["HYBRID_RRF"]["mrr"],
                "hybrid_ndcg@10": outcome["lanes"]["HYBRID_RRF"]["ndcg"].get("@10"),
            }
        )
    weights = []
    for dense_weight, sparse_weight in ((1.0, 1.0), (1.5, 1.0), (1.0, 1.5), (2.0, 1.0)):
        config = RetrievalConfig(dense_weight=dense_weight, sparse_weight=sparse_weight)
        outcome = evaluate(gold, vectors, sparse, encoder, config)
        weights.append(
            {
                "dense_weight": dense_weight,
                "sparse_weight": sparse_weight,
                "hybrid_recall@5": outcome["lanes"]["HYBRID_RRF"]["recall"].get("@5"),
                "hybrid_mrr": outcome["lanes"]["HYBRID_RRF"]["mrr"],
                "hybrid_ndcg@10": outcome["lanes"]["HYBRID_RRF"]["ndcg"].get("@10"),
            }
        )
    return {"candidate_budget": budgets, "rrf_constant": constants, "fusion_weights": weights}


if __name__ == "__main__":
    raise SystemExit(main())
