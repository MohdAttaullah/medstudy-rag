"""Reciprocal Rank Fusion.

    score(d) = SUM over lanes i of  weight_i / (k + rank_i(d))

Fusion is rank-based on purpose. A MedCPT inner product and a BM25 score are numbers on unrelated
scales with unrelated distributions; adding or averaging them would let whichever lane happens to
produce larger magnitudes dominate the ranking for reasons that have nothing to do with evidence.
Ranks are the only thing the two lanes agree on the meaning of.

Score-normalised or learned fusion is a separate strategy that would need its own calibration and
its own benchmark. It is deliberately absent rather than approximated here.
"""

from collections.abc import Sequence

from app.retrieval.model import FusedHit, Ranking


class ReciprocalRankFusion:
    """Weighted RRF with deterministic ordering.

    `k` damps the influence of the very top ranks; 60 is the conventional starting value from the
    original TREC work and is treated here as a benchmark seed, not as a calibrated result. The
    weights default to 1.0 so the production default is plain, unweighted RRF.
    """

    def __init__(self, k: int = 60) -> None:
        if k < 1:
            raise ValueError("The RRF constant must be at least 1")
        self.k = k

    def fuse(self, rankings: Sequence[Ranking], limit: int) -> tuple[FusedHit, ...]:
        totals: dict[object, float] = {}
        lanes: dict[object, list[str]] = {}
        ranks: dict[tuple[str, object], int] = {}
        scores: dict[tuple[str, object], float] = {}

        for ranking in rankings:
            # A lane that returned the same chunk twice must not be counted twice: the first
            # (best) rank is the lane's opinion, and any repeat is a duplicate to be discarded.
            seen: set[object] = set()
            for hit in ranking.hits:
                if hit.chunk_id in seen:
                    continue
                seen.add(hit.chunk_id)
                totals[hit.chunk_id] = totals.get(hit.chunk_id, 0.0) + ranking.weight / (
                    self.k + hit.rank
                )
                lanes.setdefault(hit.chunk_id, []).append(ranking.lane)
                ranks[(ranking.lane, hit.chunk_id)] = hit.rank
                scores[(ranking.lane, hit.chunk_id)] = hit.score

        # Ties are broken by chunk id so the same candidate set is produced on every run and on
        # every machine. Without it an evaluation could move a document between two positions and
        # make an unchanged system look like it had changed.
        ordered = sorted(totals.items(), key=lambda item: (-item[1], str(item[0])))
        return tuple(
            FusedHit(
                chunk_id=chunk_id,  # type: ignore[arg-type]
                fused_score=score,
                fused_rank=position,
                dense_rank=ranks.get(("DENSE", chunk_id)),
                dense_score=scores.get(("DENSE", chunk_id)),
                sparse_rank=ranks.get(("BM25", chunk_id)),
                sparse_score=scores.get(("BM25", chunk_id)),
                lanes=tuple(lanes[chunk_id]),
            )
            for position, (chunk_id, score) in enumerate(ordered[:limit], start=1)
        )
