# Retrieval evaluation

M0 supplies a small synthetic contract fixture in `bootstrap-cases.json`. It is not clinically reviewed
medical gold data and cannot substantiate accuracy. Replace/extend it with licensed, de-identified,
clinician-reviewed corpus questions before calibrating retrieval or evidence policy.

The fixture covers factual, multi-hop, table, formula, figure, unanswerable, ambiguous, conflicting,
exact-term and question-bank patterns. Each case defines expected evidence IDs and gate outcome.
Use authoritative source annotations, original page/box references and independent-source labels in
the real dataset. Split by document/topic to avoid leakage; freeze corpus, parser, chunk and index IDs.

Report Recall@K (retrieved relevant IDs / all relevant IDs), MRR (reciprocal first relevant rank),
nDCG for graded relevance, exact-term recall and authority/conflict coverage. Empty-relevance cases
measure no-answer behaviour separately; do not assign arbitrary perfect recall to empty gold sets.
Compare MedCPT, BGE-M3, sparse, fusion, reranker and optional ColBERT lanes on identical snapshots.
Report latency distributions and per-query cost with sample counts and uncertainty.

No retrieval runner or measured scores exist in M0. The structural tests only validate fixture shape
and referential consistency. Actual retrieval evals become acceptance checks as M3–M6 are implemented.
