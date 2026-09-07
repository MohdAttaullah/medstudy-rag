# Current configuration management

M10 adds [typed configuration management](configuration-management.md), including registry,
precedence, tenant revisions, previews, pending rebuild values, audit and secret handling. The
foundation notes below describe how individual policy groups originated; statements that there is
no writable API are historical and are superseded by the M10 contract.

# Configuration foundation

`Settings` is injected through `create_app`, with `MEDRAG_` environment variables and `__` nested
fields. `uv run --env-file .env ...` supplies local values; there is no import-time settings singleton.
Secrets use `SecretStr`. `.env.example` has no working credentials; the setup script generates them
into ignored `.env`. Compose explicitly maps container service addresses instead of host addresses.
Production environment is rejected until security controls exist. M1 has no writable settings API.

| Setting group | Change classification | Future persistence behavior |
|---|---|---|
| Candidate budgets, expansion, evidence policy | runtime-safe | new immutable config version for subsequent requests |
| Chunking, embeddings, vector dimensions/schema | reindex-required | staged new index; activate only after reconciliation |
| Infrastructure, identity, telemetry exporters | restart-required | deploy new service configuration |
| Generator/verifier selection | runtime-safe | validated approved provider/model registry and answer snapshot |

Selected chunk/retrieval fields expose `change_class` in Pydantic JSON Schema. Full schema-driven
settings UI, config persistence/diffs and activation logic belong to later milestones. The policy
object is frozen with cross-field bounds; retrieval policy versions are labels, not a durable registry.
Model selection requires an explicit provider/model pair and defaults to absent. No model availability
or strength is inferred from a vendor name. Later adapters choose the highest ranked *configured and
validated* production model; missing configuration causes abstention/error, never a guessed ID.

Use `MEDRAG_POLICY__DENSE_TOP_K=60` to override a benchmark candidate count. Child/parent seeds are
384/1280 tokens. None of these defaults establishes retrieval quality or medical accuracy.

Settings API reference: [Pydantic settings documentation](https://docs.pydantic.dev/latest/concepts/pydantic_settings/).

## M1 ingestion settings

`MEDRAG_INGESTION__...` supplies typed, frozen IngestionConfig fields. Each new job stores its full
JSON snapshot and version (default ingestion-m1-v1). Changing environment values requires restarting
API/worker/dispatcher; existing job snapshots and retry caps remain immutable. Increment the version
when deliberately changing policy. There is no editable configuration registry/UI yet.

| Field | Default |
|---|---|
| max_upload_bytes | 134217728 (128 MiB) |
| allowed_mime_types | application/pdf only |
| duplicate_policy | reject-within-tenant |
| max_retries | 3 |
| stream_chunk_bytes | 65536 |
| validation_timeout_seconds | 20 |
| upload_timeout_seconds | 300 |
| intent_expiry_seconds | 900 |
| storage_timeout_seconds | 20 per storage request |
| dispatch_interval_seconds | 2 |
| delivery_retry_seconds | 30 |
| dispatch_batch_size | 20 |

Delivery/recovery settings are operational values used by the current dispatcher; a job snapshot
records the settings at acceptance, not a permanently running historical dispatcher.

## M2 parsing settings

`MEDRAG_PARSING__...` supplies the typed, frozen `ParsingConfig`. Each ParseRun stores the full
JSON snapshot, the version label and a SHA-256 fingerprint of the whole policy. Reparse
idempotency keys on the **fingerprint**, not the label, so an edited threshold cannot silently
reuse a parse produced under different rules. Changing values requires restarting the worker;
existing runs keep their snapshot.

| Field | Default | Note |
|---|---|---|
| version | parsing-m2-v1 | Human label; increment on a deliberate policy change |
| parser_name | docling | Adapter selection |
| timeout_seconds | 900 | Per-document parser limit |
| max_pages | 2000 | Refused before conversion starts |
| max_concurrency | 1 | Parser processes per worker |
| parser_threads | 4 | Pinned so host core count cannot change layout prediction |
| ocr_mode | AUTO | OFF, AUTO (regions without a text layer) or FORCE (whole pages) |
| extract_tables / extract_formulas / extract_figures | true | Structured artifact extraction |
| generate_page_previews | true | Optional; a preview failure never fails a parse |
| preview_scale / preview_format | 1.5 / webp | Page preview rendering |
| figure_format | png | Extracted figure crops |
| max_artifact_bytes | 67108864 (64 MiB) | Raw parse artifact ceiling |
| lease_seconds | 1800 | Parse lease; the dispatcher releases expired leases |
| temp_dir | null | Parent for per-job temporary directories |
| thresholds.* | see below | Typed quality bounds, part of the fingerprint |

| Threshold | Default |
|---|---|
| min_non_empty_page_ratio | 0.6 |
| min_chars_per_page | 40 |
| min_elements_per_page | 0.5 |
| max_invalid_bbox_ratio | 0.02 |
| max_unlocated_element_ratio | 0.10 |
| max_malformed_table_ratio | 0.25 |
| max_suspicious_ocr_page_ratio | 0.25 |
| max_empty_formula_ratio | 0.25 |
| review_on_page_count_mismatch | true |

These defaults were chosen against synthetic fixtures. They are not calibrated on a clinical
corpus and none of them expresses a parse accuracy; they are bounds on observable ratios.
Example: `MEDRAG_PARSING__OCR_MODE=FORCE`, `MEDRAG_PARSING__THRESHOLDS__MIN_CHARS_PER_PAGE=80`.

## M3 chunking settings

`MEDRAG_CHUNKING__...` supplies the typed, frozen `ChunkingConfig`. Each ChunkRun stores the full
JSON snapshot, the version label and a SHA-256 fingerprint of the whole policy, alongside a separate
fingerprint of its normalized input. A completed dataset is reused only when the parse run, the
policy fingerprint and a freshly recomputed input fingerprint all match, so an edited target or
threshold cannot silently reuse chunks built under different rules.

| Field | Default | Note |
|---|---|---|
| version | chunking-m3-v1 | Human label; increment on a deliberate policy change |
| chunker_name / chunker_version | medical-structure / 1.0.0 | Builder selection and its behavioural version |
| tokenizer_name / tokenizer_revision | ncbi/MedCPT-Article-Encoder / d05a736d | Bundled locally; measurement only, never embedding |
| tokenizer_sha256 / tokenizer_runtime | pinned | Both are verified at load; a mismatch fails closed |
| child_target_tokens | 384 | Precise retrieval units |
| parent_target_tokens | 1280 | Structural context; must be at least the child target |
| table_max_tokens | 450 | Row-group budget before a table splits into parts |
| explanation_max_tokens | 384 | Question explanation budget before it becomes a child |
| overlap_tokens | 0 | Fixed: overlap would duplicate source text across units |
| include_hierarchy_context | true | Deterministic `Context:` prefix on retrieval text |
| include_repeated_margins | false | Repeated running heads/feet are excluded from chunking, never deleted |
| repeated_margin_min_pages / margin_fraction / margin_max_characters | 2 / 0.08 / 160 | Repetition and geometry bounds for that exclusion |
| formula_neighbour_elements / figure_neighbour_elements | 1 / 1 | Only parser-linked neighbours may join an artifact chunk |
| max_source_elements / max_source_characters | 100000 / 20000000 | Refused before building starts |
| timeout_seconds / lease_seconds | 300 / 360 | The lease must exceed the timeout |
| receipt_recovery_seconds | 30 | Recovers the receipt-to-claim crash window |
| thresholds.tiny_tokens | 24 | Below this a separate child is flagged as a tiny orphan |
| thresholds.max_tiny_ratio / max_oversized_ratio | 0.7 / 0.05 | Dataset-level review bounds |
| thresholds.max_atomic_tokens | 2048 | An atomic unit above this is routed to review, never cut |

These defaults were chosen against synthetic fixtures. They are not calibrated on a clinical corpus
and none of them expresses a chunk quality or retrieval accuracy; they are bounds on observable
counts. Example: `MEDRAG_CHUNKING__CHILD_TARGET_TOKENS=320`,
`MEDRAG_CHUNKING__THRESHOLDS__TINY_TOKENS=32`.

## M4 embedding settings

`MEDRAG_EMBEDDING__...` supplies the typed, frozen `EmbeddingConfig`. Two fingerprints are derived
from it. The **semantics fingerprint** covers only what a vector means and identifies the
`EmbeddingVersion`, so changing a batch size reuses the vector space while changing pooling or the
model creates a new one. The **policy fingerprint** covers the whole configuration and is recorded
on each run.

Fields typed as `Literal` cannot be changed by environment variable at all. That is deliberate:
pooling, normalization, dimension, metric, the input limit and the truncation policy change what
every stored vector means, and a change to any of them needs a code change, a benchmark and an ADR
rather than a redeploy.

| Field | Default | Note |
|---|---|---|
| version | embedding-m4-v1 | Human label; increment on a deliberate policy change |
| model_id / model_revision | ncbi/MedCPT-Article-Encoder / d05a736d… | Pinned; `main` is never used |
| model_checksum / verify_model_checksum | a5d5ffe4… / true | Weights are verified before the model loads |
| embedding_dimension | 768 | Fixed; also the index vector size |
| pooling_strategy | CLS | Fixed; the released MedCPT article representation |
| normalization | NONE | Fixed; unnormalized vectors are why similarity is DOT |
| distance_metric | DOT | Fixed; maximum inner product |
| max_input_tokens | 512 | Fixed; the model maximum |
| truncation_policy | REJECT | Fixed; an over-long chunk fails loudly and is never shortened |
| input_builder_version | medcpt-two-field-v1 | Fixed; part of the input hash |
| max_context_characters | 300 | Context-field budget; trimmed from the front, deterministic |
| eligible_chunk_types | nine retrieval types | `TEXT_PARENT` is rejected by validation |
| embed_parent_chunks | false | Fixed; parents are context units, not retrieval units |
| batch_size / max_batch_tokens | 16 / 8192 | Inference batching |
| torch_threads | 4 | CPU inference; no CUDA dependency |
| reuse_existing_embeddings | true | Reuse requires an exact input-hash match |
| model_cache_dir / offline | null / false | Set to the provisioned cache and true in the worker |
| timeout_seconds / lease_seconds | 3600 / 4200 | The lease must exceed the timeout |
| max_chunks | 200000 | Refused before embedding starts |

`MEDRAG_INDEX__...` supplies the frozen `IndexConfig`.

| Field | Default | Note |
|---|---|---|
| version / schema_version | index-m4-v1 / v1 | Schema version is part of the collection name |
| collection_prefix / alias | medrag_chunks / medrag_chunks_active | Physical name also carries the semantics fingerprint |
| dense_vector_name | medcpt_dense | Named so sparse and late-interaction vectors can join later |
| upsert_batch_size / verify_batch_size | 64 / 128 | Batched writes and read-back |
| verify_vectors / verify_sample_ratio | true / 1.0 | Full read-back verification by default |
| request_timeout_seconds / upsert_retries | 60 / 3 | Transient index failures are retried |
| payload_indexes | seven keys | Documented in [vector index](vector-index.md) |
| tenant_payload_key | tenant_id | Must be a payload index; validation enforces it |

Example: `MEDRAG_EMBEDDING__BATCH_SIZE=32`, `MEDRAG_EMBEDDING__OFFLINE=true`,
`MEDRAG_INDEX__UPSERT_BATCH_SIZE=128`. Changing the embedding model or any vector semantics is a
**reindex-required** change: it creates a new EmbeddingVersion and a new physical collection, and
the alias switches only after the replacement verifies.

`MEDRAG_DEV_PRINCIPALS` is a JSON list of secret bearer-token/user/tenant/role mappings, provisioned
by scripts/init_dev_auth.py. Empty configuration denies all data access. Roles are reader, curator,
admin. Production mode remains rejected. Never return this setting from a public config endpoint.