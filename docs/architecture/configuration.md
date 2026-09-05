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
records the settings at acceptance, not a permanently running historical dispatcher. Chunker,
embedding and index versions will be added with the corresponding stages; none of those run yet.

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

`MEDRAG_DEV_PRINCIPALS` is a JSON list of secret bearer-token/user/tenant/role mappings, provisioned
by scripts/init_dev_auth.py. Empty configuration denies all data access. Roles are reader, curator,
admin. Production mode remains rejected. Never return this setting from a public config endpoint.