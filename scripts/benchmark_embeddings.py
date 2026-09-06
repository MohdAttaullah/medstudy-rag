"""Measure embedding and index throughput on this machine.

    uv run --extra embedding python scripts/benchmark_embeddings.py [--chunks 200] [--batch 16]

Reports model load time, embeddings per second at the configured batch size, index upsert
throughput and peak process memory. These are measurements of this host with this synthetic
corpus, not a service-level objective and not a claim about production capacity.
"""

import argparse
import sys
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.embedding_config import EmbeddingConfig, IndexConfig  # noqa: E402
from app.embeddings.inputs import ChunkSource, build  # noqa: E402
from app.embeddings.medcpt import MedCPTArticleEmbedder  # noqa: E402
from app.evaluation.embeddings import load_gold  # noqa: E402
from app.vectorindex.model import VectorPoint, VectorSchema  # noqa: E402

GOLD = ROOT / "backend/tests/fixtures/embedding/gold.json"


def memory_mb() -> float | None:
    try:  # Linux and other platforms exposing resource usage
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1024 if peak > 1_000_000 else peak / 1024
    except Exception:  # noqa: BLE001 - Windows has no resource module
        try:
            import ctypes
            import ctypes.wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            )
            return counters.PeakWorkingSetSize / (1024 * 1024)
        except Exception:  # noqa: BLE001 - reported as unavailable rather than guessed
            return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=int, default=200)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/models/embeddings")
    parser.add_argument("--index-url", default="")
    arguments = parser.parse_args()

    config = EmbeddingConfig(
        model_cache_dir=arguments.cache, offline=True, batch_size=arguments.batch
    )
    cases, _ = load_gold(GOLD)
    usable = [case for case in cases if case.expect_eligible and not case.expect_over_limit]

    # Repeat the fixtures with a distinct suffix so every input is unique and nothing is cached.
    sources = [
        ChunkSource(
            chunk_id=uuid5(NAMESPACE_URL, f"benchmark/{index}"),
            chunk_type=case.chunk_type,
            retrieval_text=f"{case.retrieval_text} Repetition {index} for throughput measurement.",
            document_title=case.document_title,
            hierarchy=tuple(case.hierarchy),
            caption=case.caption,
        )
        for index in range(arguments.chunks)
        for case in [usable[index % len(usable)]]
    ]
    inputs = tuple(build(source, config) for source in sources)

    model = MedCPTArticleEmbedder(config)
    started = time.perf_counter()
    model.load()
    load_seconds = time.perf_counter() - started

    tokens = sum(item.token_count for item in model.measure(inputs))
    started = time.perf_counter()
    vectors: list = []
    for offset in range(0, len(inputs), config.batch_size):
        vectors.extend(model.embed_documents(inputs[offset : offset + config.batch_size]))
    embed_seconds = time.perf_counter() - started

    print(f"chunks:              {len(inputs)}")
    print(f"batch size:          {config.batch_size}")
    print(f"torch threads:       {config.torch_threads}")
    print(f"average tokens:      {tokens / len(inputs):.1f}")
    print(f"model load:          {load_seconds:.1f} s")
    print(f"embedding time:      {embed_seconds:.1f} s")
    print(f"embeddings/second:   {len(inputs) / embed_seconds:.1f}")
    print(f"tokens/second:       {tokens / embed_seconds:.0f}")

    if arguments.index_url:
        from app.vectorindex.qdrant import QdrantVectorIndex

        index_config = IndexConfig(collection_prefix="medrag_benchmark")
        index = QdrantVectorIndex(arguments.index_url)
        schema = VectorSchema(
            collection=index_config.collection(config.semantics_fingerprint),
            vector_name=index_config.dense_vector_name,
            dimension=config.embedding_dimension,
            distance=config.distance_metric,
            payload_indexes=index_config.payload_indexes,
            tenant_key=index_config.tenant_payload_key,
        )
        index.ensure_schema(schema)
        points = tuple(
            VectorPoint(
                point_id=uuid5(NAMESPACE_URL, f"benchmark-point/{position}"),
                chunk_id=vector.chunk_id,
                vector=vector.values,
                payload={"tenant_id": "benchmark", "chunk_type": "TEXT_CHILD"},
            )
            for position, vector in enumerate(vectors)
        )
        started = time.perf_counter()
        size = index_config.upsert_batch_size
        for offset in range(0, len(points), size):
            index.upsert(schema, points[offset : offset + size])
        upsert_seconds = time.perf_counter() - started
        print(f"upsert batches:      {(len(points) + size - 1) // size}")
        print(f"index upsert time:   {upsert_seconds:.1f} s")
        print(f"points/second:       {len(points) / upsert_seconds:.1f}")
        index.client.delete_collection(schema.collection)

    peak = memory_mb()
    print(f"peak process memory: {f'{peak:.0f} MB' if peak else 'unavailable on this platform'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
