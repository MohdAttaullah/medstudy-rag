---
name: rag-evaluation
description: Create or run evidence-based retrieval and answer evaluations with reproducible datasets and metrics.
---

# Rag Evaluation

Read docs/evals/retrieval-evaluation.md and answer-evaluation.md. Record dataset/corpus/index/config/prompt/model versions and expert annotation provenance. Keep synthetic contract tests separate from clinically reviewed gold data.

Include factual, multi-hop, table, formula, original-figure, absent, ambiguous, conflict, exact-term and assessment-key cases. Separate retrieval misses from generation failures. Define metric denominators and report undefined metrics honestly.

Evaluate citation identity/page resolution, unsupported claims and abstention precision/recall. Calibrate on held-out data without document leakage. Report executed commands, counts, uncertainty and failures; never tune prompts to conceal weak retrieval.
