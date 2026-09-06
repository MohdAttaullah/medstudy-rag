# Embeddings (M4)

Embedding turns the retrieval-eligible chunks of one validated chunk dataset into versioned dense
vectors. It runs after [document chunking](document-chunking.md) and hands off to
[the vector index](vector-index.md). **An embedded version is not answerable.** There is no query
encoder, no search, no reranking and no generation at this milestone.

## Model and pinning

| | |
|---|---|
| Model | `ncbi/MedCPT-Article-Encoder` (document side) |
| Revision | `d05a736da4bb84ee4057b7f7999485be6ed85465` |
| Weights | `model.safetensors`, SHA-256 `a5d5ffe4…845b0f3`, verified before load |
| Tokenizer | the same revision — the identical `tokenizer.json` M3 already pins for chunk measurement |
| Pooling | last hidden state of `[CLS]` |
| Dimension | 768 |
| Normalization | none |
| Similarity | inner product (`DOT`) |
| Max input | 512 tokens |
| dtype / device | float32 / CPU |

These reproduce the released MedCPT article-side representation. Mean pooling and L2 normalization
are deliberately *not* used: either would change what every stored vector means and would need its
own benchmark, ADR and EmbeddingVersion. Because vectors are unnormalized, maximum inner product is
the correct similarity, which is why collections are created with `DOT` distance.

`main` is never used. The revision, the tokenizer revision and the weight checksum are pinned in
`EmbeddingConfig`, and the adapter refuses to load anything else. The loaded model is additionally
cross-checked: its reported `hidden_size` must equal the configured dimension and its position
limit must cover the configured maximum.

Only `backend/app/embeddings/medcpt.py` imports torch, transformers or huggingface_hub. Services
depend on the `EmbeddingModel` protocol in `model.py`, so replacing the encoder is one module.

## Offline operation

`scripts/provision_embedding_model.py` downloads exactly the pinned revision into a controlled
cache and verifies the weight checksum. The worker then runs with `MEDRAG_EMBEDDING__OFFLINE=true`
against `/home/medrag/models/embeddings`, backed by the `embedding-models` named volume. A model
that is absent from the cache fails closed with `EMBEDDING_MODEL_UNAVAILABLE_OFFLINE` rather than
reaching the network. No user request depends on a runtime download.

This is deliberately stricter than the M2 parser, whose weights are still fetched on first use.

## Retrieval eligibility

Not every chunk is a first-stage retrieval unit.

Eligible: `TEXT_CHILD`, `LIST`, `TABLE`, `TABLE_PART`, `FORMULA`, `FIGURE_CONTEXT`, `QUESTION`,
`QUESTION_EXPLANATION`, `OTHER_STRUCTURED`.

**`TEXT_PARENT` is excluded by default and cannot be enabled through configuration.** A parent is
the union of its children, so indexing both would return a whole section where a precise passage was
wanted and would double-count the same source text in any future ranking. Parents are retained for
context expansion in a later milestone; if they are ever embedded they must use a separate vector
name or an explicit retrieval role, and stay disabled by default.

Question-bank chunks *are* indexed, and their payload preserves `source_type`, `authority_level`
and the question's own explicit-answer status, so a later retrieval stage can filter or weight
assessment material differently. **Being indexed never promotes a question key to reference
authority.**

## Embedding input

`backend/app/embeddings/inputs.py` builds a deterministic two-field input matching MedCPT's
article representation:

```
field 1 (context)  Document title > chapter > section > artifact caption
field 2 (body)     the M3 retrieval representation, verbatim
```

The context is assembled only from the chunk's own persisted hierarchy (M3's `HIERARCHY`-role
source mappings) and its artifact caption. Nothing is summarized, paraphrased or invented, and no
LLM is involved. When the context exceeds its character budget it is trimmed **from the front**, so
the nearest heading and the caption survive; the trim is deterministic and inside the input hash.
An empty context stays an empty first field rather than being folded into the body, so the field
boundary never moves.

The embedding input is a derived representation, not source content. The chain stays:

```
original PDF → parsed element → chunk source text → retrieval representation → embedding input → vector
```

`ChunkEmbedding.input_hash` is a SHA-256 over the builder version, model id and revision, the field
separator and both fields — that is, over exactly what the model sees.

## Truncation

MedCPT accepts 512 tokens. The policy is `REJECT`, and it is the only policy implemented.

Every input is measured with the model's own tokenizer *before* inference. If any exceeds the limit
the run fails with `EMBEDDING_INPUT_TOO_LONG`, a CRITICAL finding is persisted naming each
offending chunk id and its token count, and **nothing is embedded**. The remedy is to rechunk.

Silent truncation is refused because it would index a chunk whose vector represents only part of
the evidence the chunk claims to carry — a retrieval result would then cite text the vector never
saw. A `truncated` column exists on `chunk_embeddings` and is constrained to false, so a truncated
vector is unstorable even by mistake.

## Determinism, stated precisely

* **Repeated inference on one host, same batch layout** — byte identical. Verified.
* **Batched versus single inference** — numerically equivalent, *not* byte identical. Padding
  differs, so float arithmetic differs in the last bits: measured max absolute delta below 1e-6 and
  cosine agreement to 1.0 within 1e-6 on the fixtures. Verified and asserted with a tolerance.
* **Across hosts and CPU backends** — not claimed. See
  [the M4 report](../verification/m4.md) for the measured host/container comparison.

`vector_checksum` is a SHA-256 of the canonical little-endian float32 encoding. It is therefore a
**byte identity for storage integrity** — it detects corruption and confirms that what came back
from the index is what was computed — and never a claim of reproducibility across batch layouts or
machines, nor any measure of embedding quality. Vector *reuse* keys on `input_hash` and the
embedding version, never on a vector checksum.

## Versioning

`EmbeddingVersion` describes what a vector *means*: provider, model, revision, tokenizer revision,
checksum, dimension, pooling, normalization, metric, input limit, dtype and input-builder version.
It is keyed on a SHA-256 **semantics fingerprint** over exactly those fields, so changing a batch
size or a thread count reuses the same vector space while changing pooling, the model or the input
representation creates a new one. Many `EmbeddingRun`s across many documents share one version.

`EmbeddingRun` is one durable attempt over one chunk dataset: counts, fingerprints, a fenced lease,
metrics and a safe error. A chunk dataset may have several historical runs; old vectors are never
rewritten as though a new model had produced them.

`ChunkEmbedding` records the metadata for one vector — point id, vector name, input hash, vector
checksum, dimension, token count, norm — while the dense array itself lives in the index. That is
what makes reconciliation possible: without a durable expected set, "the index looks fine" is an
assertion about a system that cannot be audited.

## Reuse and resumability

A vector is reused only when the input hash matches exactly, in the same tenant, under the same
embedding version, from a succeeded run. Similar-looking text never shares a vector. A reused
vector is read back from the index and its checksum re-verified before it is written into the new
run, so a reused point is written from real data rather than from an assumption.

Duplicate task delivery is idempotent: the outbox receipt is claimed once, one pending-or-running
run per version is enforced by a partial unique index, and a run already claimed is not rebuilt.

## Configuration and resources

CPU-first; no CUDA dependency is introduced. Batch size, max batch tokens, torch threads, timeout
and lease are configuration. Batching is real — inference runs in batches and index upserts run in
batches; nothing issues one request per chunk.

## Not in M4

Query-side encoding (`MedCPT-Query-Encoder`), search, BM25, sparse or hybrid retrieval, fusion,
reranking, late interaction, provider calls and answering. No `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY` is used or required anywhere in this milestone.
