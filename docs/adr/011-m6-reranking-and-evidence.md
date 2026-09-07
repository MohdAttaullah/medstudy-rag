# ADR-011: MedCPT reranking and bounded source evidence

Status: Accepted for M6. Extends ADR-010 without changing stored indexes, ingestion states, or the generation boundary.

The authorized API orchestrates hybrid retrieval, full canonical chunk hydration, MedCPT CrossEncoder reranking, and deterministic evidence construction. The existing private retrieval runtime keeps Query Encoder and CrossEncoder warm. Authorization and active-corpus decisions remain in the API. Article Encoder remains an ingestion workload.

The primary model is `ncbi/MedCPT-Cross-Encoder`, revision `71caf65d4927987813984f54c284405a13fcca49`. All seven required files are SHA-256 verified before local-only loading. The official checkpoint supplies PyTorch weights; loading uses `weights_only=True` and `trust_remote_code=False`. No alternate checkpoint or conversion branch is substituted. See the [official revision](https://huggingface.co/ncbi/MedCPT-Cross-Encoder/tree/71caf65d4927987813984f54c284405a13fcca49).

Representation v1 pairs the normalized original query with full persisted M3 retrieval text, including pair special tokens within the 512-token maximum. Overflow fails explicitly. Raw float32 logits rank candidates descending; ties use original fused rank then UUID. Scores never establish medical confidence or evidence sufficiency.

Frozen, fingerprinted query policies remain request-scoped. No relational M6 entity or empty migration is needed. The M6 candidate cap is separate from M5's public result cap: up to 40 candidates, default 20, with five anchors. M5's 40/40 lanes, analyzer, BM25 parameters, RRF constant and weights remain unchanged. Pools 10/20 show equal top-five quality on the small synthetic fixture; 20 remains an uncalibrated conservative default.

Evidence admits ranked anchors before optional context. Atomic table parts, formulas and source question records stay whole or are omitted with a budget finding. Text overlap is removed using source offsets scoped to document version. Distinct sources are preserved. Incomplete text fragments may acquire bounded missing parent context. One sibling per side is eligible only within the same parent, version and run with source-order continuity. The 0/0 versus 1/1 synthetic comparison supports the latter within the existing 256-token neighbour and 512-token parent limits.

Tables, formulas, figures and questions use canonical M2/M3 artifacts and explicit source relationships. Original figures remain inspectable even without captions. Question evidence records every contributing chunk. Nothing interprets a figure, invents a key, rewrites evidence, or resolves a conflict by model preference.

Corpus identity is rechecked after retrieval, inference and expansion hydration. Every source lookup reapplies tenant/run/version scope. Drift or missing provenance fails closed. Final assembly uses the hydrated immutable snapshot without further source lookup. The default budget is 4096 WordPiece tokens, 20 blocks, and 1024 tokens per block; these are inspection limits, not a future generation-model prompt budget.

Rollback restores M5 application/configuration and leaves its indexes valid. The independent reranker cache may be retained. No schema downgrade is required. M7 and all generation functionality remain outside this decision.
