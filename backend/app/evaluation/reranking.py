"""Ranking regressions, separate from missing corpus evidence."""

from uuid import UUID


def failures(
    relevant: set[UUID], pool: list[UUID], baseline: list[UUID], reranked: list[UUID], k: int = 5
) -> list[str]:
    if not relevant:
        return ["CORPUS_LACKS_EVIDENCE"]
    result = []
    if relevant - set(pool):
        result.append("FIRST_STAGE_MISS")
    if relevant.intersection(baseline[:k]) - set(reranked[:k]):
        result.append("RERANKER_REGRESSION")
    if baseline[:k] == reranked[:k]:
        result.append("RERANKER_NO_GAIN")
    return result
