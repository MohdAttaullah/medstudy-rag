# Retrieval architecture — planned

Lane A uses MedCPT Query Encoder for questions and Article Encoder for corpus chunks. Lane B uses
BM25/sparse lexical retrieval for exact terminology. BGE-M3 is a benchmark alternative. ColBERT is
disabled unless an evaluation demonstrates sufficient benefit for its storage, latency and cost.
Candidate budgets start at 40 per lane in typed configuration; these are uncalibrated seeds.

Apply authorization and active-manifest filters to every lane before candidate fusion. Fuse by RRF,
with versioned parameters; rerank using MedCPT Cross-Encoder. Six final evidence blocks is an initial
benchmark seed (target range 5–8), not a fixed medical sufficiency rule. Do not compare raw scores
across models without calibration. Empty/failed lanes must be observable and assessed by policy.

Expand selected children to useful parents, bounded neighbours and associated table/figure/formula
elements. Preserve IDs/provenance, deduplicate overlap, exclude unauthorized/staged data and enforce
a configured token budget. Generated query rewrites and captions can assist retrieval, never support
claims by themselves. Query text is untrusted, including prompt-injection attempts in corpus elements.

Source authority configuration distinguishes guidelines, reference books, textbooks, course material,
question banks, papers, keys and other sources. Authority includes edition/year and specialty; recency
alone does not settle conflict. Assessment keys do not override textbooks automatically. Material
conflict among authoritative evidence prevents ordinary generation until policy resolves its handling.

No retrieval implementation or measured retrieval performance exists in M0. See
[retrieval evaluation](../evals/retrieval-evaluation.md) before changing chunk/index parameters.
