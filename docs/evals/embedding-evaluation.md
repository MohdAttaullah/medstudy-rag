# Embedding technical-quality evaluation

This measures one narrow question: **does the embedding system behave the way its configuration
says it does?** The pinned revision is loaded, inputs are built deterministically, vectors have the
declared shape and are finite, the token limit is enforced rather than silently applied, batching
agrees with single inference, and repeated inference on one host is stable.

It is **not** retrieval evaluation. There is no query, no index, no ranking and no Recall@K, MRR or
nDCG — those become meaningful only once query-side retrieval exists, in the next milestone. It is
also not a medical accuracy measure.

It is separate from [parsing evaluation](parsing-evaluation.md) and
[chunk evaluation](chunking-evaluation.md), and from [retrieval evaluation](retrieval-evaluation.md)
and [answer evaluation](answer-evaluation.md), which remain unimplemented.

## Dataset

`backend/tests/fixtures/embedding/gold.json` (schema `embedding-gold-m4-v1`). Thirteen synthetic
chunks and two similarity pairs. Every fixture is original synthetic content written for this
repository: no copyrighted medical corpus, and no wording intended to be medically meaningful.

| Case | What it exercises |
|---|---|
| plain-text | An ordinary child chunk |
| plain-text-duplicate | Identical content must produce an identical input hash |
| changed-representation | Changed body text must not collide with the original |
| changed-hierarchy | Changed context must not collide with the original |
| long-but-valid | A long chunk that still fits inside 512 tokens (measured 468) |
| over-the-limit | A chunk that genuinely exceeds 512 tokens (measured 698) and must be rejected |
| table-part | A structured table representation with repeated headers |
| formula | An atomic formula representation |
| figure-context | Figure caption context, with no visual reasoning |
| question | A question with options and an explicit source answer |
| list | A list kept with its heading |
| parent-not-indexed | `TEXT_PARENT` must be ineligible |
| cross-tenant-shaped | An additional eligible structured type |

## Running it

```powershell
uv run --extra embedding python scripts/evaluate_embeddings.py
```

Fully offline: the pinned model must already be in the local cache (see
`scripts/provision_embedding_model.py`). Nothing is downloaded, no index is touched and no query is
issued. It prints the model, revision and vector semantics, then per-case and per-check counts, and
exits non-zero on any failure.

## What each case group asserts

* **model-pinning** — the loaded model reports the pinned id, revision and weight checksum, and the
  configured dimension, pooling, normalization, metric and input limit.
* **eligibility** — each chunk type is eligible or not exactly as policy says; `TEXT_PARENT` is not.
* **input-determinism** — rebuilding a source yields the same fields and hash; identical content
  collides deliberately, different content never does; the body is the retrieval representation
  verbatim.
* **token-limit** — measured counts match the expected over/under-limit classification.
* **truncation-policy** — an over-long input is refused, not shortened.
* **vector-shape** — 768 dimensions, finite, non-zero, within the token limit, passing the sanity
  rules.
* **repeatability** — a second pass on the same host produces byte-identical vectors.
* **batch-equivalence** — batched and single inference agree numerically (max delta < 1e-4, cosine
  agreement within 1e-6 of 1.0). Byte identity across batch layouts is *not* asserted, because it
  is not true: padding changes the last bits of float arithmetic.
* **similarity-sanity** — a near paraphrase scores above obviously unrelated prose under the
  configured inner product.

## About the similarity check

It is a coarse "not catastrophically broken" test, exactly two pairs, and **nothing in the
architecture is tuned against it**. It must never be quoted as retrieval accuracy. Note that
because MedCPT article vectors are unnormalized, inner products between unrelated passages are
still large in absolute terms; only the *ordering* is asserted, and the margin on these fixtures is
narrow (roughly 74 versus 67 on the paraphrase pair). Whether these vectors retrieve well is
unanswerable until a query encoder, an index search and a gold retrieval set exist.

## What this does not establish

Nothing here is clinical validation, and no accuracy percentage should be quoted from this harness.
Synthetic fixtures do not represent real medical prose, tables or question layouts. The token
targets inherited from chunking and the batch sizes chosen here were set against these fixtures and
need re-tuning against a real corpus.
