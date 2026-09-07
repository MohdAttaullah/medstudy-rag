# M6 reranking

`POST /api/v1/retrieval/rerank` requires `retrieval:search`. It accepts a query and optional M5
filters, never tenant IDs, candidate IDs, or policy overrides. Its typed response preserves M5
lane diagnostics, adds reranked candidates and an EvidenceSet, and keeps `answering_enabled=false`.
The M5 `/retrieval/search` endpoint retains its original behavior.

`services/evidence.py` orchestrates. `reranking/model.py` defines the provider-neutral protocol;
`reranking/medcpt.py` isolates PyTorch/Transformers and `reranking/remote.py` validates private HTTP
results, identity, candidate membership, finite scores and exact input hashes. No generative model
or provider SDK participates.

Model: `ncbi/MedCPT-Cross-Encoder`; model/tokenizer revision
`71caf65d4927987813984f54c284405a13fcca49`.
Weight SHA-256: `61d5ccd48869e03500544525fc231641d7daa9ba267b202c82724750038dc1e0`.
Tokenizer JSON SHA-256: `6e046044df8a2fcedb10607075dca187cae61d806c0d80a96c5b81017edc90c9`.
All seven file checksums live in `core/reranking_config.py` and are checked before offline loading.
The official PyTorch checkpoint loads with `weights_only=True` and `trust_remote_code=False`.
Libraries are locked in `uv.lock` and their loaded versions recorded in the trace.

Representation v1 pairs the M5-normalized original query with full stored M3 retrieval text,
never the API preview. Pair special tokens count toward 512 tokens. Overflow rejects the request;
there is no silent truncation, rewriting or query expansion. Scores are raw float32 logits,
ordered descending with original fused rank then UUID ties. They are ranking diagnostics,
not medical confidence, probabilities of correctness, or sufficiency judgments.

Default policy: 20 fused candidates, five anchors. The M6 cap is separate from M5's public cap;
M5's 40/40 lane sizes, analyzer, BM25 and RRF stay unchanged. Runtime defaults: CPU, float32,
eight pairs per batch, four threads, 90-second HTTP timeout. Model/cache/runtime changes require
restart; candidate/expansion/budget policies are query-side and require no reindexing. Policies
are frozen and fingerprinted in every trace.

The existing private retrieval service keeps Query Encoder and CrossEncoder warm. Missing cache,
checksum/identity mismatch, overlength input, inference failure, nonfinite output and HTTP timeout
fail closed. There is no automatic M5 fallback. M6 persists neither query nor source text and
puts neither in telemetry. See [ADR-011](../adr/011-m6-reranking-and-evidence.md),
[context policy](context-expansion.md), and [verification](../verification/m6.md).
