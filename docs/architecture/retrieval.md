# Hybrid retrieval (M5)

The implemented first-stage path is authenticated query -> resolve active corpus -> MedCPT dense
search and/or PostgreSQL BM25 -> RRF -> canonical provenance hydration -> evidence candidates.
DENSE_ONLY, BM25_ONLY and HYBRID_RRF are supported. All modes use the same aligned active corpus;
dense-only is not a bypass around incomplete lexical ingestion. See [ADR-010](../adr/010-m5-hybrid-retrieval.md).

The pinned Query Encoder is ncbi/MedCPT-Query-Encoder at
`d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc`, weight SHA-256
`19d78c0d5eaee2f81e6c47c5425bbadcc0c6af016cbb5da4a000d64e59d6e342`.
It uses CLS, 768 unnormalized float32 dimensions and DOT, compatible with M4's Article Encoder.
The default query limit is 64 tokens including special tokens. Longer queries are rejected.
Normalization folds Unicode, typography and whitespace; it never expands abbreviations or rewrites
questions. The query tokenizer JSON checksum is identical to the article tokenizer JSON; the
complete query checkpoint has its own pinned tokenizer revision and supporting files.

The MedCPT adapter is the only query module importing torch/transformers. The API can use the
internal HTTP adapter to a separate query process. Deployment uses a provisioned offline named
volume. No request downloads a model. The in-process vector cache is bounded, instance-scoped to
frozen encoder semantics, and stores no query text. Traces carry hashes and model/config IDs.

PostgreSQL resolves every tenant's active VERIFIED IndexRun and SparseIndex by document version,
requiring matching ChunkRun IDs and RETRIEVAL_READY state. Archived, staged, failed and superseded
versions are excluded. Mixed analyzer/embedding versions fail closed. Qdrant receives tenant and
active embedding-run filters before search; BM25 receives tenant and active sparse-index filters.
Optional facets cover document/version, chunk/source type, authority, subject and specialty.
Hydration checks the exact chunk run and re-resolves corpus identity before returning results.

RRF uses `sum(weight/(k+rank))`; defaults are k=60, weights=1 and 40 candidates per lane, with 20
final candidates. Ties use chunk UUID. BM25 defaults k1=1.2 and b=0.75. These are measured seeds,
not calibrated thresholds. Required-lane failure is fail-closed by default. ALLOW_DENSE_ONLY may
explicitly tolerate lexical failure and emits a warning. No score is medical confidence.

POST `/api/v1/retrieval/search` requires retrieval:search. GET `/api/v1/retrieval/status`, encoder
versions and sparse-index inspection expose metadata with authorization. Search returns candidates,
lane ranks/scores, matched terms and a trace, plus answering_enabled=false. It has no answer,
confidence or vector field. The Retrieval Inspector supports mode selection, lane comparisons,
trace inspection and candidate -> chunk -> source-page navigation.

Retrieval is not evidence sufficiency. Negative queries may still return candidates. No reranking,
parent/neighbour expansion, medical interpretation, generation or Ask answering exists in M5.

## M6 extension

A separately authorized rerank/evidence endpoint preserves the M5 diagnostic API and policies.
Full persisted chunk text is rehydrated for CrossEncoder inference and the M6 pool cap is versioned
separately. Corpus identity protection continues through expansion. See [reranking](reranking.md)
and [evidence construction](context-expansion.md). Ask remains disabled.

## M7 extension

A separately authorized draft endpoint runs the M5 and M6 stages unchanged, then the evidence
sufficiency gate, then at most one grounded generation. M5 lane semantics, analyzer, BM25
parameters, RRF constant and weights remain untouched, and no retrieval score participates in the
sufficiency decision. See [sufficiency](sufficiency.md) and [generation](generation.md).

## M8 extension

A separately authorized answer endpoint runs the M5–M7 stages unchanged, then verifies every
material claim in the draft before anything is released. No retrieval, fusion or reranker score
participates in verification, and rank never decides which of two disagreeing sources is correct.
See [verification](verification.md).
