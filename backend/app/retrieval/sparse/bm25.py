"""BM25 scoring and an in-memory lexical index.

The scoring function is Okapi BM25 with the Lucene inverse document frequency:

    idf(t)   = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
    score(d) = SUM over query terms t of
               idf(t) * (tf(t,d) * (k1 + 1)) / (tf(t,d) + k1 * (1 - b + b * len(d) / avgdl))

Two consequences of computing this at query time rather than baking weights into the index:

* `k1` and `b` are runtime-safe. Changing them re-ranks immediately and rebuilds nothing.
* `N`, `df` and `avgdl` are taken over exactly the corpus a query is allowed to see — one
  tenant's active, verified, version-aligned indexes. Collection-wide statistics from another
  tenant or from a superseded index never influence a score.

The Lucene idf form is chosen because it is never negative: with the classical Robertson form a
term appearing in more than half the corpus contributes a negative score, so a document could be
penalised for containing a query term. That is a poor property for a recall-first first stage.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from app.core.retrieval_config import SparseAnalyzerConfig
from app.retrieval.model import LaneHit, Ranking, RetrievalCorpus, RetrievalFilters
from app.retrieval.sparse.analyzer import frequencies, query_terms


@dataclass(frozen=True, slots=True)
class CorpusStatistics:
    """Collection-level statistics for exactly the corpus slice being searched."""

    document_count: int
    total_length: int
    document_frequency: Mapping[str, int]

    @property
    def average_length(self) -> float:
        if self.document_count <= 0:
            return 0.0
        return self.total_length / self.document_count


@dataclass(frozen=True, slots=True)
class Posting:
    """One (term, chunk) occurrence with the facets the sparse lane can filter on."""

    chunk_id: UUID
    term: str
    term_frequency: int
    length: int
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    chunk_type: str | None = None
    source_type: str | None = None
    authority_level: str | None = None
    subject: str | None = None
    specialty: str | None = None


def inverse_document_frequency(document_frequency: int, document_count: int) -> float:
    if document_count <= 0:
        return 0.0
    frequency = max(0, min(document_frequency, document_count))
    return math.log(1 + (document_count - frequency + 0.5) / (frequency + 0.5))


def term_score(
    term_frequency: int,
    length: int,
    idf: float,
    *,
    k1: float,
    b: float,
    average_length: float,
) -> float:
    """One term's BM25 contribution to one document."""
    if term_frequency <= 0:
        return 0.0
    normalizer = 1.0 - b + b * (length / average_length if average_length > 0 else 1.0)
    return idf * (term_frequency * (k1 + 1.0)) / (term_frequency + k1 * normalizer)


def _matches(posting: Posting, filters: RetrievalFilters | None) -> bool:
    if filters is None or not filters.active:
        return True
    checks = (
        (filters.document_ids, posting.document_id),
        (filters.document_version_ids, posting.document_version_id),
        (filters.chunk_types, posting.chunk_type),
        (filters.source_types, posting.source_type),
        (filters.authority_levels, posting.authority_level),
        (filters.subjects, posting.subject),
        (filters.specialties, posting.specialty),
    )
    return all(not allowed or value in allowed for allowed, value in checks)


def rank(
    postings: Iterable[Posting],
    statistics: CorpusStatistics,
    query: Sequence[str],
    *,
    k1: float,
    b: float,
    top_k: int,
    filters: RetrievalFilters | None = None,
) -> tuple[tuple[UUID, float, tuple[str, ...]], ...]:
    """Score the supplied postings and return the top candidates.

    Ties are broken by chunk id so a ranking is reproducible across runs and processes; without
    it, two documents with identical scores could swap places between evaluations and make a
    measured difference look like a retrieval change.
    """
    wanted = set(query)
    idf = {
        term: inverse_document_frequency(
            statistics.document_frequency.get(term, 0), statistics.document_count
        )
        for term in wanted
    }
    average = statistics.average_length
    totals: dict[UUID, float] = {}
    matched: dict[UUID, list[str]] = {}
    for posting in sorted(postings, key=lambda p: (str(p.chunk_id), p.term)):
        if posting.term not in wanted or not _matches(posting, filters):
            continue
        contribution = term_score(
            posting.term_frequency,
            posting.length,
            idf[posting.term],
            k1=k1,
            b=b,
            average_length=average,
        )
        if contribution <= 0:
            continue
        totals[posting.chunk_id] = totals.get(posting.chunk_id, 0.0) + contribution
        matched.setdefault(posting.chunk_id, []).append(posting.term)
    ordered = sorted(totals.items(), key=lambda item: (-item[1], str(item[0])))
    return tuple(
        (chunk_id, score, tuple(sorted(matched[chunk_id]))) for chunk_id, score in ordered[:top_k]
    )


@dataclass
class InMemorySparseIndex:
    """A lexical index held in memory, used by the offline evaluation harness and unit tests.

    It shares the analyzer, the statistics model and the scoring function with the durable
    PostgreSQL implementation, so an evaluation measures the same BM25 the service runs. What it
    deliberately does *not* share is durability, versioning and tenant enforcement — those live in
    the database implementation and are proven by integration tests, not here.
    """

    config: SparseAnalyzerConfig
    postings: list[Posting] = field(default_factory=list)
    lengths: dict[UUID, int] = field(default_factory=dict)
    document_frequency: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        chunk_id: UUID,
        text: str,
        *,
        document_id: UUID | None = None,
        document_version_id: UUID | None = None,
        chunk_type: str | None = None,
        source_type: str | None = None,
        authority_level: str | None = None,
        subject: str | None = None,
        specialty: str | None = None,
    ) -> int:
        counts, length = frequencies(text, self.config)
        self.lengths[chunk_id] = length
        for term, frequency in counts.items():
            self.document_frequency[term] = self.document_frequency.get(term, 0) + 1
            self.postings.append(
                Posting(
                    chunk_id=chunk_id,
                    term=term,
                    term_frequency=frequency,
                    length=length,
                    document_id=document_id,
                    document_version_id=document_version_id,
                    chunk_type=chunk_type,
                    source_type=source_type,
                    authority_level=authority_level,
                    subject=subject,
                    specialty=specialty,
                )
            )
        return length

    @property
    def statistics(self) -> CorpusStatistics:
        return CorpusStatistics(
            document_count=len(self.lengths),
            total_length=sum(self.lengths.values()),
            document_frequency=self.document_frequency,
        )

    def search(
        self,
        query: str,
        *,
        corpus: RetrievalCorpus | None = None,
        top_k: int = 20,
        filters: RetrievalFilters | None = None,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> Ranking:
        import time

        started = time.perf_counter()
        wanted = query_terms(query, self.config)
        scored = rank(
            self.postings,
            self.statistics,
            wanted,
            k1=k1,
            b=b,
            top_k=top_k,
            filters=filters,
        )
        hits = tuple(
            LaneHit(chunk_id=chunk_id, score=score, rank=position, matched_terms=terms)
            for position, (chunk_id, score, terms) in enumerate(scored, start=1)
        )
        return Ranking(
            lane="BM25",
            hits=hits,
            duration_ms=(time.perf_counter() - started) * 1000,
            scanned=len(self.postings),
        )
