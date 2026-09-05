# ADR-003: Hybrid candidate retrieval with mandatory reranking

Status: Accepted target; implementations scheduled for M5–M6.

Use MedCPT query/article encoders in their correct roles, BM25/sparse retrieval, RRF fusion and
MedCPT Cross-Encoder reranking. Expand selected evidence semantically within a versioned budget.
ColBERT remains optional and disabled until its marginal value is measured; benchmark BGE-M3.

Dense-only search can miss exact terminology; lexical-only search misses semantic variants. More
context cannot compensate for missing evidence. Measure lane recall, fused recall, reranking,
authority/conflict coverage, latency and cost on the same corpus snapshot. Candidate counts and
chunk sizes are configurable seeds, not accepted calibration results. No retrieval scores exist in M0.
