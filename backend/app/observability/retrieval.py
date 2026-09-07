"""Retrieval metrics.

Labels are bounded values only — mode, lane, declared error code. A query, a query hash, a chunk
id, a document id and a tenant id are all deliberately absent: the first would put medical
questions in a metrics store, and the rest would give the series unbounded cardinality.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram

# Query encoding is a model forward pass and lexical search is a database scan; they live on
# different scales, so one shared bucket set would hide both.
DURATIONS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
CANDIDATES = (0, 1, 5, 10, 20, 40, 80, 160, 320)


class RetrievalMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.sufficiency = Counter(
            "evidence_sufficiency_decisions_total",
            "M7 sufficiency gate decisions",
            ["status"],
            registry=registry,
        )
        self.evidence_stage = Histogram(
            "evidence_stage_duration_seconds",
            "M6 query stage duration",
            ["stage"],
            buckets=DURATIONS,
            registry=registry,
        )
        self.queries = Counter(
            "retrieval_queries_total",
            "Retrieval requests that produced a candidate set",
            ["mode"],
            registry=registry,
        )
        self.failures = Counter(
            "retrieval_failures_total",
            "Retrieval requests that ended in a declared failure",
            ["mode", "code"],
            registry=registry,
        )
        self.encoding = Histogram(
            "query_encoding_duration_seconds",
            "Time to turn a question into a query vector",
            buckets=DURATIONS,
            registry=registry,
        )
        self.dense = Histogram(
            "dense_search_duration_seconds",
            "Time spent in the vector index",
            buckets=DURATIONS,
            registry=registry,
        )
        self.sparse = Histogram(
            "sparse_search_duration_seconds",
            "Time spent in lexical search",
            buckets=DURATIONS,
            registry=registry,
        )
        self.fusion = Histogram(
            "fusion_duration_seconds",
            "Time spent fusing lane rankings",
            buckets=DURATIONS,
            registry=registry,
        )
        self.hydration = Histogram(
            "hydration_duration_seconds",
            "Time spent resolving candidates to canonical sources",
            buckets=DURATIONS,
            registry=registry,
        )
        self.total = Histogram(
            "retrieval_duration_seconds",
            "End-to-end retrieval time",
            ["mode"],
            buckets=DURATIONS,
            registry=registry,
        )
        self.dense_candidates = Histogram(
            "dense_candidates_total",
            "Candidates returned by the dense lane",
            buckets=CANDIDATES,
            registry=registry,
        )
        self.sparse_candidates = Histogram(
            "sparse_candidates_total",
            "Candidates returned by the lexical lane",
            buckets=CANDIDATES,
            registry=registry,
        )
        self.fused_candidates = Histogram(
            "fused_candidates_total",
            "Candidates surviving fusion",
            buckets=CANDIDATES,
            registry=registry,
        )
