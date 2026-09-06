"""Offline retrieval-quality evaluation.

This module measures **retrieval**: whether the evidence a question needs is found, and how high
it appears. It measures nothing about medical correctness, and a high Recall@5 here says only that
the right passage was retrieved from a small synthetic corpus — never that an answer would be
right, and never that the system may answer at all.

Three deliberate choices about the numbers:

* **Negative cases are scored separately.** A question with no relevant evidence in the corpus has
  no recall to compute, and averaging it in as either 0.0 or 1.0 would move the headline figure
  for a reason unrelated to retrieval. It is reported as its own diagnostic instead.
* **nDCG is only reported where graded judgments exist.** Manufacturing grades from binary labels
  would produce a number that looks more informative than the data behind it.
* **Every lane is measured on the same corpus snapshot**, with the same analyzer, the same
  encoders and the same fixture, so a difference between lanes is a difference between lanes.
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

RECALL_DEPTHS = (1, 3, 5, 10, 20)
PRECISION_DEPTHS = (1, 5, 10)
NDCG_DEPTHS = (5, 10)

# Fixture chunk labels are stable strings; this maps them to the UUID identity the retrieval code
# uses, deterministically, so a gold file never has to carry generated identifiers.
FIXTURE_NAMESPACE = uuid5(NAMESPACE_URL, "medrag/evaluation/retrieval")


def fixture_id(label: str) -> UUID:
    return uuid5(FIXTURE_NAMESPACE, label)


@dataclass(frozen=True, slots=True)
class GoldChunk:
    label: str
    document: str
    chunk_type: str
    text: str
    page_start: int
    page_end: int
    hierarchy: tuple[str, ...] = ()
    caption: str | None = None

    @property
    def chunk_id(self) -> UUID:
        return fixture_id(self.label)


@dataclass(frozen=True, slots=True)
class GoldDocument:
    label: str
    title: str
    source_type: str
    authority_level: str
    subject: str | None = None
    specialty: str | None = None


@dataclass(frozen=True, slots=True)
class GoldCase:
    """One evaluation query.

    `relevant` maps a chunk label to a relevance grade: 3 for a passage that directly contains the
    evidence, 1 for one that is genuinely related and useful. An empty mapping is a negative case
    — the corpus does not contain the answer — and is scored separately.
    """

    id: str
    category: str
    query: str
    relevant: Mapping[str, int] = field(default_factory=dict)
    graded: bool = False
    notes: str = ""
    expect_error: str | None = None

    @property
    def negative(self) -> bool:
        return not self.relevant

    @property
    def relevant_ids(self) -> set[UUID]:
        return {fixture_id(label) for label in self.relevant}

    def grade(self, chunk_id: UUID) -> int:
        for label, value in self.relevant.items():
            if fixture_id(label) == chunk_id:
                return value
        return 0


@dataclass(frozen=True, slots=True)
class GoldSet:
    version: str
    notice: str
    documents: tuple[GoldDocument, ...]
    chunks: tuple[GoldChunk, ...]
    cases: tuple[GoldCase, ...]

    def document(self, label: str) -> GoldDocument:
        for entry in self.documents:
            if entry.label == label:
                return entry
        raise KeyError(label)


def load_gold(path: Path) -> GoldSet:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return GoldSet(
        version=str(payload["dataset_version"]),
        notice=str(payload["notice"]),
        documents=tuple(
            GoldDocument(
                label=str(item["label"]),
                title=str(item["title"]),
                source_type=str(item["source_type"]),
                authority_level=str(item["authority_level"]),
                subject=item.get("subject"),
                specialty=item.get("specialty"),
            )
            for item in payload["documents"]
        ),
        chunks=tuple(
            GoldChunk(
                label=str(item["label"]),
                document=str(item["document"]),
                chunk_type=str(item["chunk_type"]),
                text=str(item["text"]),
                page_start=int(item["page_start"]),
                page_end=int(item["page_end"]),
                hierarchy=tuple(item.get("hierarchy", ())),
                caption=item.get("caption"),
            )
            for item in payload["chunks"]
        ),
        cases=tuple(
            GoldCase(
                id=str(item["id"]),
                category=str(item["category"]),
                query=str(item["query"]),
                relevant={str(key): int(value) for key, value in item.get("relevant", {}).items()},
                graded=bool(item.get("graded", False)),
                notes=str(item.get("notes", "")),
                expect_error=item.get("expect_error"),
            )
            for item in payload["cases"]
        ),
    )


# ------------------------------------------------------------------------------------- metrics


def recall_at(ranked: Sequence[UUID], relevant: set[UUID], depth: int) -> float:
    """Fraction of the relevant evidence that appears in the top `depth`.

    Undefined without relevant evidence, so a negative case must never be passed here; the caller
    scores those separately rather than inventing a value.
    """
    if not relevant:
        raise ValueError("Recall is undefined when no evidence is relevant")
    found = len(set(ranked[:depth]) & relevant)
    return found / len(relevant)


def precision_at(ranked: Sequence[UUID], relevant: set[UUID], depth: int) -> float:
    if depth <= 0:
        return 0.0
    window = ranked[:depth]
    if not window:
        return 0.0
    return len(set(window) & relevant) / len(window)


def reciprocal_rank(ranked: Sequence[UUID], relevant: set[UUID]) -> float:
    """1 / rank of the first relevant candidate; 0.0 when none was retrieved."""
    for position, chunk_id in enumerate(ranked, start=1):
        if chunk_id in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at(ranked: Sequence[UUID], case: GoldCase, depth: int) -> float:
    """Normalised discounted cumulative gain with exponential gain.

    Only meaningful when the case carries graded judgments; the caller checks `case.graded`.
    """
    gains = [(2 ** case.grade(chunk_id) - 1) for chunk_id in ranked[:depth]]
    dcg = sum(gain / math.log2(position + 1) for position, gain in enumerate(gains, start=1))
    ideal = sorted((2**grade - 1 for grade in case.relevant.values()), reverse=True)[:depth]
    best = sum(gain / math.log2(position + 1) for position, gain in enumerate(ideal, start=1))
    return dcg / best if best > 0 else 0.0


@dataclass
class LaneScores:
    """Accumulated metrics for one lane over one dataset."""

    lane: str
    recall: dict[int, list[float]] = field(default_factory=dict)
    precision: dict[int, list[float]] = field(default_factory=dict)
    ndcg: dict[int, list[float]] = field(default_factory=dict)
    reciprocal: list[float] = field(default_factory=list)
    per_category: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    negatives: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    # Top-1 scores split by whether the corpus actually contains the answer. M7 will need to know
    # whether a lane's score separates the two at all before any sufficiency threshold is drawn.
    positive_top_scores: list[float] = field(default_factory=list)
    negative_top_scores: list[float] = field(default_factory=list)

    def observe(
        self, case: GoldCase, ranked: Sequence[UUID], duration_ms: float, top_score: float | None
    ) -> None:
        self.latencies_ms.append(duration_ms)
        if case.negative:
            # No recall to compute. What is recorded instead is what the system actually did,
            # which is the evidence M7's sufficiency gate will need to be calibrated against.
            self.negatives.append(
                {
                    "case": case.id,
                    "category": case.category,
                    "candidates": len(ranked),
                    "top_score": top_score,
                }
            )
            if top_score is not None:
                self.negative_top_scores.append(top_score)
            return
        if top_score is not None:
            self.positive_top_scores.append(top_score)
        relevant = case.relevant_ids
        for depth in RECALL_DEPTHS:
            value = recall_at(ranked, relevant, depth)
            self.recall.setdefault(depth, []).append(value)
            self._category(case, f"recall@{depth}", value)
        for depth in PRECISION_DEPTHS:
            value = precision_at(ranked, relevant, depth)
            self.precision.setdefault(depth, []).append(value)
        rank = reciprocal_rank(ranked, relevant)
        self.reciprocal.append(rank)
        self._category(case, "mrr", rank)
        if case.graded:
            for depth in NDCG_DEPTHS:
                value = ndcg_at(ranked, case, depth)
                self.ndcg.setdefault(depth, []).append(value)
                self._category(case, f"ndcg@{depth}", value)
        if rank == 0.0:
            self.failures.append(
                {
                    "case": case.id,
                    "category": case.category,
                    "expected": sorted(case.relevant),
                    "retrieved": [str(chunk_id) for chunk_id in ranked[:10]],
                }
            )

    def _category(self, case: GoldCase, metric: str, value: float) -> None:
        self.per_category.setdefault(case.category, {}).setdefault(metric, []).append(value)

    def summary(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "scored_cases": len(self.reciprocal),
            "recall": {f"@{depth}": _mean(values) for depth, values in sorted(self.recall.items())},
            "precision": {
                f"@{depth}": _mean(values) for depth, values in sorted(self.precision.items())
            },
            "ndcg": {f"@{depth}": _mean(values) for depth, values in sorted(self.ndcg.items())},
            "mrr": _mean(self.reciprocal),
            "latency_ms": {
                "p50": _percentile(self.latencies_ms, 50),
                "p95": _percentile(self.latencies_ms, 95),
                "mean": _mean(self.latencies_ms),
            },
            "per_category": {
                category: {metric: _mean(values) for metric, values in sorted(metrics.items())}
                for category, metrics in sorted(self.per_category.items())
            },
            "negative_cases": self.negatives,
            "failed_cases": self.failures,
            "errors": self.errors,
            "top_score_separation": self.separation(),
        }

    def separation(self) -> dict[str, Any]:
        """Whether this lane's top score tells an answerable question from an unanswerable one.

        Reported as raw statistics with no threshold attached. A lane whose negative maximum sits
        above its positive minimum cannot support a score cut-off, and saying so plainly is more
        useful than a number that implies a gate exists.
        """
        positives, negatives = self.positive_top_scores, self.negative_top_scores
        if not positives or not negatives:
            return {"separable": None, "reason": "insufficient cases"}
        return {
            "positive_min": round(min(positives), 6),
            "positive_mean": round(sum(positives) / len(positives), 6),
            "negative_max": round(max(negatives), 6),
            "negative_mean": round(sum(negatives) / len(negatives), 6),
            "separable": min(positives) > max(negatives),
        }


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (percentile / 100) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


# --------------------------------------------------------------------------- failure analysis

FAILURE_CLASSES = {
    "LEXICAL_MISMATCH": "The evidence shares no analyzed term with the question.",
    "SEMANTIC_MISMATCH": "The dense lane ranked the evidence outside the candidate budget.",
    "BOTH_LANES_MISSED": "Neither lane retrieved the evidence within its budget.",
    "FUSION_DISPLACED": "A lane retrieved the evidence but fusion pushed it past the cut-off.",
    "CORPUS_LACKS_EVIDENCE": "The gold evidence is not present in the indexed corpus.",
}


def classify(
    case: GoldCase,
    dense: Sequence[UUID],
    sparse: Sequence[UUID],
    fused: Sequence[UUID],
    corpus_ids: set[UUID],
) -> str:
    """A first, mechanical guess at why a case failed. It is a triage label, not a diagnosis.

    Deliberately conservative: it never proposes an architecture change, and an isolated failure
    is a fixture to investigate rather than a reason to retune the pipeline.
    """
    relevant = case.relevant_ids
    if not relevant & corpus_ids:
        return "CORPUS_LACKS_EVIDENCE"
    in_dense = bool(relevant & set(dense))
    in_sparse = bool(relevant & set(sparse))
    if (in_dense or in_sparse) and not relevant & set(fused):
        return "FUSION_DISPLACED"
    if not in_dense and not in_sparse:
        return "BOTH_LANES_MISSED"
    if not in_sparse:
        return "LEXICAL_MISMATCH"
    return "SEMANTIC_MISMATCH"
