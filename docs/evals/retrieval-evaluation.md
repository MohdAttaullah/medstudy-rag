# Retrieval evaluation

A retrieval runner and measured baseline scores exist from M5. Read the numbers with the caveat
below before reading the numbers.

**The corpus is synthetic.** `backend/tests/fixtures/retrieval/gold.json` was written for this
repository to make Recall@K, MRR and nDCG computable over text whose correct answers are known by
construction. It is not licensed medical content, it has not been reviewed by a clinician, and no
figure derived from it is evidence about clinical performance. Replace it with a licensed,
de-identified, clinician-reviewed corpus before any retrieval number is treated as a real
measurement. The M0 contract fixture in `bootstrap-cases.json` remains a separate shape-only
fixture and is unchanged.

## The harness

`scripts/evaluate_retrieval.py` runs the same analyzer, the same BM25 scorer, the same RRF
implementation and the same two pinned MedCPT encoders the service runs, over an in-memory index
built from the gold corpus. It never calls a language model and produces no answers.

What it deliberately does not exercise is PostgreSQL, Qdrant, tenancy and authorization. Keeping
those out is what lets an evaluation run with no stack up; they are proven instead by
`backend/tests/test_m5_integration.py` and `scripts/smoke_m5.py`, which drive the durable path
end to end. The report says so explicitly in its `corpus_identity` block rather than leaving the
run, index and version identifiers silently absent.

Each report records the git commit, the dataset version **and the SHA-256 of the dataset file**,
both encoder revisions and semantics fingerprints, the analyzer fingerprint, the full retrieval
configuration and its fingerprint, and the library versions the encoder itself reported. Without
those a retrieval number is not reproducible and should not be quoted.

## What is reported

Recall@1/3/5/10/20, MRR, Precision@1/5/10, and nDCG@5/10 for cases carrying graded judgments.
Everything is reported per lane — dense, BM25, hybrid RRF — on the same corpus snapshot, and again
per category, because an average hides the case where one lane fails completely.

Empty-relevance cases are scored separately and never assigned a recall value: recall over an
empty gold set is undefined, and filling it with 0.0 or 1.0 would move the headline figure for a
reason unrelated to retrieval. What is recorded for them instead is the top score each lane
produced, next to the same statistic over the positive cases, so a later milestone can see whether
a lane's score separates answerable from unanswerable questions at all before any threshold is
drawn.

Every case also gets a per-query diagnostic — the rank at which each lane found each expected
chunk, the top scores, and the candidate counts — not only the failures. A case that succeeded at
rank 1 in one lane and rank 9 in another is exactly the evidence reranking will need, and it is
invisible in an aggregate.

Failures are classified mechanically as `LEXICAL_MISMATCH`, `SEMANTIC_MISMATCH`,
`BOTH_LANES_MISSED`, `FUSION_DISPLACED` or `CORPUS_LACKS_EVIDENCE`. The label is triage, not a
diagnosis, and an isolated failure is a fixture to investigate rather than a reason to retune the
pipeline.

## Query-boundary reporting

The query encoder's 64-token limit bounds the dense and hybrid lanes only. BM25 never sees the
encoder, so a question too long to encode is still perfectly searchable lexically. The report
records that asymmetry per case in `query_boundary`: which lanes were available, the observed
token count against the limit, and how many candidates the lexical lane still returned. Reporting
such a case as a whole-system failure would misdescribe the system.

## Rules for changing anything

Do not tune `k1`, `b`, the RRF constant, the fusion weights, the analyzer or the candidate budgets
to improve a score on this synthetic corpus. The first run is a baseline. If a real defect is
found, fix the defect and report the before and after.

Freeze the corpus, parser, chunk, embedding and index identifiers when comparing runs. Split by
document and topic to avoid leakage once a real corpus exists. Report latency distributions with
sample counts.

BGE-M3, a reranker and an optional ColBERT lane remain future comparisons on identical snapshots;
none exists yet, and no number for them may be quoted.

The measured M5 baseline, its limitations and the host/Linux comparison are in
[the M5 verification report](../verification/m5.md).
