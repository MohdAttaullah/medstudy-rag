# Sparse retrieval (M5)

Lexical data lives in PostgreSQL, separate from Qdrant's dense vectors. The canonical biomedical
analyzer preserves connected identifiers, case-exact terms and numeric/unit expressions, and emits
whole/part variants under a versioned policy. Default stopwords are empty; there is no stemming,
synonym expansion or abbreviation expansion.

SparseIndexVersion records analyzer semantics. SparseIndex records each durable attempt and its
ChunkRun lineage, counts, verification, fingerprint, lease and activation. SparseDocument stores
one eligible chunk's facets and length. SparsePosting stores raw term frequency; SparseTerm stores
document/total frequency. SparseValidationFinding records reconciliation outcomes. Foreign keys,
checks and triggers protect membership, historical records and active selection.

READY_FOR_RETRIEVAL -> SPARSE_INDEXING -> VERIFYING_SPARSE_INDEX -> RETRIEVAL_READY. A sparse-index
outbox message shares the existing job/generation/kind uniqueness rule. The dispatcher recovers
expired leases and received but unclaimed messages. Explicit reindex-sparse consumes the existing
bounded retry budget and needs ingestion:reindex; custom analyzer policy additionally needs admin
permission. No automatic retry silently changes policy.

Verification derives the expected set from the active successful dense embedding run, checks all
observed IDs and counts, excludes TEXT_PARENT, and reconciles posting and term aggregates. Activation
supersedes the previous active row and activates the new VERIFIED row atomically. Completed records
cannot be edited; activation time is written once. Failed replacements retain the previous active
index, but the version's failed processing state excludes it from retrieval until repaired.

BM25 computes tenant-corpus IDF from active index aggregates and uses stored lengths/frequencies.
Facet filters constrain eligible candidates; IDF describes the active tenant corpus rather than a
facet-specific miniature corpus. Posting scans exceeding the configured cap fail instead of
silently truncating. Accumulation and score ties are deterministic. This implementation loads
bounded postings into Python; it is a development baseline, not an internet-scale lexical engine.
