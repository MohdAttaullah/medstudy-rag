---
name: retrieval-quality
description: Evaluate and change chunking, embedding, retrieval, reranking or context expansion quality.
---

# Retrieval Quality

Read docs/architecture/retrieval.md and docs/evals/retrieval-evaluation.md. Freeze corpus, config and evaluation splits before a comparison. Use MedCPT query/article encoders in their correct roles; never mix incompatible vectors.

Compare dense and sparse lanes, RRF and mandatory reranking independently. Measure exact terminology, authority/conflict coverage and unanswerable cases. Keep ColBERT optional until measured marginal gain warrants its cost.

Apply authorization and active-version filters to every lane and expansion step. Expand only semantically related provenance-bearing context within budget. Report Recall@K, MRR, graded nDCG where valid, latency and cost without claiming synthetic fixtures are medical validation.
