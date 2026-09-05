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
records the settings at acceptance, not a permanently running historical dispatcher. Parser/chunker/
embedding/index versions will be added with the corresponding stages. No such stages run in M1.

`MEDRAG_DEV_PRINCIPALS` is a JSON list of secret bearer-token/user/tenant/role mappings, provisioned
by scripts/init_dev_auth.py. Empty configuration denies all data access. Roles are reader, curator,
admin. Production mode remains rejected. Never return this setting from a public config endpoint.