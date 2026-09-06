# ADR-010: M5 hybrid retrieval and relational lexical indexes

Status: Accepted for M5. Supersedes the planned sparse-storage choice in ADR-009; the dense
Qdrant representation is unchanged. M6 reranking and expansion remain future work.

The existing implementation uses the pinned MedCPT Query Encoder, PostgreSQL BM25 and weighted
Reciprocal Rank Fusion. PostgreSQL stores raw term frequencies, lengths and aggregate document
frequencies. This keeps IDF scoped to the tenant's active corpus instead of every tenant and
superseded run sharing a Qdrant collection. Chunk IDs are shared across both lanes. No Qdrant
sparse vector or alternate collection is created. Analyzer changes require rebuilding; k1/b,
candidate budgets and RRF weights are runtime ranking settings.

A sparse build uses a durable outbox and fenced lease. It indexes exactly the active successful
EmbeddingRun's ChunkEmbedding set. Reconciliation checks identity, counts, tenant, chunk-run
lineage, eligibility, posting membership and term aggregates. Only a verified aligned index may
activate. The old active row becomes SUPERSEDED in the same transaction as new activation;
a failed replacement preserves history and the old active row. Activation timestamps cannot be
rewritten. Receipt-before-claim crashes are recovered by the dispatcher. Downgrade refuses live
M5 job states; it never silently translates them.

Corpus resolution precedes both lanes and requires active VERIFIED dense and sparse indexes on
the same ChunkRun, RETRIEVAL_READY version state, and an unarchived document. Mixed vector spaces
or analyzer versions fail closed. Every lane is scoped to the principal's tenant and resolved
run IDs. Hydration rechecks corpus identity and reads canonical text and provenance from PostgreSQL.

The query model runs in a separate, internal-only HTTP service with an offline provisioned cache.
CLS pooling, 768 dimensions, unnormalized float32 and DOT compatibility preserve the released
query/article pairing. The API never receives client tenant IDs or exposes dense vectors. Query
text is neither logged nor retained in the bounded vector cache. Long queries are rejected before
inference; no rewrite, expansion or silent truncation occurs.

RRF sums weight/(k+rank), with deterministic chunk-ID ties. Raw lane scores are diagnostics and
are never added together. Required-lane failures abort by default; optional dense-only degradation
is explicit in configuration and response warnings. Scores do not establish evidence sufficiency.

M5 ends at RETRIEVAL_READY. Database searchable flags remain false, READY is unreachable, Ask
remains disabled, and responses contain candidates with answering_enabled=false. No generation,
reranking, context expansion or medical evidence gate is implemented.
