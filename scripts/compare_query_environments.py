"""Compare query vectors and rankings across Windows and Linux."""

import argparse
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.core.embedding_config import EmbeddingConfig  # noqa: E402
from app.core.retrieval_config import QueryEncoderConfig  # noqa: E402
from app.embeddings.inputs import ChunkSource, build  # noqa: E402
from app.embeddings.medcpt import MedCPTArticleEmbedder  # noqa: E402
from app.evaluation.retrieval import load_gold  # noqa: E402
from app.retrieval.query.medcpt import MedCPTQueryEncoder  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/models/embeddings")
    parser.add_argument("--query-cache", type=Path)
    parser.add_argument("--emit", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.emit:
        gold = load_gold(ROOT / "backend/tests/fixtures/retrieval/gold.json")
        policy = QueryEncoderConfig(
            model_cache_dir=args.query_cache or args.cache, offline=True, cache_size=0
        )
        encoder = MedCPTQueryEncoder(policy)
        queries = [case.query for case in gold.cases if not case.expect_error][:11]
        vectors = [encoder.encode_queries([query])[0] for query in queries]
        article_policy = EmbeddingConfig(model_cache_dir=args.cache, offline=True)
        article = MedCPTArticleEmbedder(article_policy)
        inputs = [
            build(
                ChunkSource(
                    chunk_id=chunk.chunk_id,
                    chunk_type=chunk.chunk_type,
                    retrieval_text=chunk.text,
                    document_title=gold.document(chunk.document).title,
                    hierarchy=chunk.hierarchy,
                    caption=chunk.caption,
                ),
                article_policy,
            )
            for chunk in gold.chunks
        ]
        articles = [
            v.values
            for start in range(0, len(inputs), 8)
            for v in article.embed_documents(tuple(inputs[start : start + 8]))
        ]
        result = {
            "platform": platform.platform(),
            "revision": policy.model_revision,
            "tokenizer": policy.tokenizer_checksum,
            "semantics": policy.semantics_fingerprint,
            "libraries": encoder.specification.library_versions,
            "queries": queries,
            "vectors": [v.values for v in vectors],
            "checksums": [v.checksum for v in vectors],
            "articles": articles,
        }
        args.emit.parent.mkdir(parents=True, exist_ok=True)
        args.emit.write_text(json.dumps(result), encoding="utf-8")
        print(f"Emitted {len(vectors)} query vectors and {len(articles)} fixed corpus vectors.")
    elif args.compare:
        left, right = [json.loads(p.read_text(encoding="utf-8")) for p in args.compare]
        assert left["queries"] == right["queries"]
        assert left["revision"] == right["revision"] and left["tokenizer"] == right["tokenizer"]

        def ranking(query):
            return sorted(
                range(len(right["articles"])),
                key=lambda i: (
                    -sum(a * b for a, b in zip(query, right["articles"][i], strict=True)),
                    i,
                ),
            )

        deltas = [
            max(abs(a - b) for a, b in zip(x, y, strict=True))
            for x, y in zip(left["vectors"], right["vectors"], strict=True)
        ]
        result = {
            "inputs": len(deltas),
            "same_revision": True,
            "same_tokenizer": True,
            "revision": left["revision"],
            "byte_identical": sum(
                a == b for a, b in zip(left["checksums"], right["checksums"], strict=True)
            ),
            "max_absolute_delta": max(deltas),
            "per_query_max_delta": deltas,
            "top_10_ranking_agreement": sum(
                ranking(a)[:10] == ranking(b)[:10]
                for a, b in zip(left["vectors"], right["vectors"], strict=True)
            ),
            "ranking_reference": "Both query sets ranked against the same Linux article vectors",
            "host_libraries": left["libraries"],
            "linux_libraries": right["libraries"],
        }
        print(json.dumps(result, indent=2))
        if args.report:
            args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    else:
        parser.error("Use --emit or --compare")


if __name__ == "__main__":
    main()
