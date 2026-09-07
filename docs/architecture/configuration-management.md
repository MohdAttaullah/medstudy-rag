# M10 configuration management

The administration API projects an explicit allowlist from the existing M3–M9 typed policies.
It does not serialize Settings or edit environment variables. Registry implementation:
`backend/app/configuration/registry.py`; decision: [ADR-015](../adr/015-m10-controlled-configuration.md).

## Source of truth and scope

1. Immutable code requirements always apply.
2. A committed tenant revision overrides only registered RUNTIME_SAFE fields.
3. Startup/environment configuration remains effective for all other fields and supplies defaults
   for runtime fields without an override.
4. Code defaults apply where startup configuration is absent.

Each request resolves one revision before its pipeline starts. All policy-bearing services receive
copies, retaining their existing adapters and the M5–M9 decision order. No service-wide policy is
mutated. Later commits affect subsequent requests; the in-flight request keeps its immutable snapshot.
Ask stores revision, fingerprint and all allowlisted effective values with the turn. Older turns have
NULL snapshots rather than fabricated configuration history. Existing evidence/verification traces
continue to carry their stage fingerprints.

Tenant admins can read and write tenant policies. Shared model runtimes, ingestion limits and
process identity are SYSTEM scope and read-only: the development RBAC system has no system-operator
role. No client body can name a tenant, global scope, lifecycle, model registry, endpoint or secret.

## Lifecycle and pending values

Runtime changes activate together in one transaction. A revision must contain either runtime changes
or rebuild proposals; mixed batches fail. Full Pydantic group validation runs on every changed
policy group before any commit. NaN, Infinity, unknown names, wrong types and unsafe literals are
rejected. Editing RRF changes rank fusion, never direct addition of lane scores.

Beyond the policies' own validators M10 asserts only constraints the underlying milestones already
hold, and nothing that merely looks tidier. Two apply: a reranking candidate pool larger than
`dense_top_k + sparse_top_k` is refused because M5's `RetrievalConfig` declares that invalid and M6
assigns `candidate_top_k` into that field through `model_copy`, which does not re-run validators;
and both fusion weights at zero is refused because M6 and Ask always search with `HYBRID_RRF`
whatever `retrieval.mode` says. Notably absent is any relation between
`evidence_budget.max_total_tokens` and `max_tokens_per_block`: M6 applies the two caps
independently during assembly, so a total below the per-block ceiling is a valid, tested M6 state
meaning the total binds first. Asserting otherwise here would let a configuration surface silently
redefine verified M6 semantics.

Chunking/analyzer proposals are stored as DESIRED / PENDING_REBUILD. Their effective policy and all
active chunk/index manifests remain unchanged. An operator must use the established versioned
rechunk/reindex or lexical rebuild and verification workflow outside M10. M10 neither orchestrates
rebuilds nor declares proposals active after an environment match. Pending proposal history is
retained; automated fulfillment/reconciliation is not implemented. Rebuild scope is the tenant's
corpus, not mutation of an active physical index.

Embedding/query model identities and vector semantics are visibly reindex-impacting but read-only:
the current implementation supports only its pinned model pair. The CrossEncoder and worker runtime
settings require external deployment/restart and are read-only. Values describe the API's startup
contract; M10 does not claim to control or independently attest remote process deployment state.

## Registry

The table lists every exposed key, its scope and lifecycle; editable values are still validated by
the backend. READ ONLY rows do not offer an edit action. Section labels are returned by the server.

| Section | Key | Lifecycle | Scope | Editable |
|---|---|---|---|---|
| Retrieval | `retrieval.dense_top_k` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.sparse_top_k` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.final_top_k` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.rrf_k` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.dense_weight` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.sparse_weight` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.bm25_k1` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.bm25_b` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.mode` | RUNTIME_SAFE | TENANT | Yes |
| Retrieval | `retrieval.degradation_policy` | IMMUTABLE | TENANT | No |
| Retrieval | `sparse_analyzer.case_policy` | REINDEX_REQUIRED | TENANT | Yes |
| Retrieval | `sparse_analyzer.compound_policy` | REINDEX_REQUIRED | TENANT | Yes |
| Retrieval | `sparse_analyzer.min_term_length` | REINDEX_REQUIRED | TENANT | Yes |
| Retrieval | `sparse_analyzer.max_term_length` | REINDEX_REQUIRED | TENANT | Yes |
| Reranking | `reranking.candidate_top_k` | RUNTIME_SAFE | TENANT | Yes |
| Reranking | `reranking.final_top_k` | RUNTIME_SAFE | TENANT | Yes |
| Reranking | `reranker.batch_size` | RESTART_REQUIRED | SYSTEM | No |
| Reranking | `reranker.torch_threads` | RESTART_REQUIRED | SYSTEM | No |
| Reranking | `reranker.request_timeout_seconds` | RESTART_REQUIRED | SYSTEM | No |
| Evidence | `expansion.parent_enabled` | RUNTIME_SAFE | TENANT | Yes |
| Evidence | `expansion.max_parent_tokens` | RUNTIME_SAFE | TENANT | Yes |
| Evidence | `expansion.max_expansions_per_anchor` | RUNTIME_SAFE | TENANT | Yes |
| Evidence | `expansion.previous_siblings` | RUNTIME_SAFE | TENANT | Yes |
| Evidence | `expansion.next_siblings` | RUNTIME_SAFE | TENANT | Yes |
| Evidence | `expansion.max_neighbour_tokens` | RUNTIME_SAFE | TENANT | Yes |
| Evidence | `evidence_budget.max_total_tokens` | RUNTIME_SAFE | TENANT | Yes |
| Evidence | `evidence_budget.max_blocks` | RUNTIME_SAFE | TENANT | Yes |
| Evidence | `evidence_budget.max_tokens_per_block` | RUNTIME_SAFE | TENANT | Yes |
| Chunking | `chunking.child_target_tokens` | RECHUNK_REINDEX_REQUIRED | TENANT | Yes |
| Chunking | `chunking.parent_target_tokens` | RECHUNK_REINDEX_REQUIRED | TENANT | Yes |
| Chunking | `chunking.table_max_tokens` | RECHUNK_REINDEX_REQUIRED | TENANT | Yes |
| Chunking | `chunking.explanation_max_tokens` | RECHUNK_REINDEX_REQUIRED | TENANT | Yes |
| Chunking | `chunking.figure_neighbour_elements` | RECHUNK_REINDEX_REQUIRED | TENANT | Yes |
| Chunking | `chunking.formula_neighbour_elements` | RECHUNK_REINDEX_REQUIRED | TENANT | Yes |
| Chunking | `chunking.overlap_tokens` | IMMUTABLE | TENANT | No |
| Models | `embedding.model_id` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `embedding.model_revision` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `query_encoder.model_id` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `query_encoder.model_revision` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `reranker.model_id` | RESTART_REQUIRED | SYSTEM | No |
| Models | `reranker.model_revision` | RESTART_REQUIRED | SYSTEM | No |
| Models | `embedding.pooling_strategy` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `embedding.embedding_dimension` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `embedding.normalization` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `embedding.distance_metric` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `embedding.input_builder_version` | REINDEX_REQUIRED | SYSTEM | No |
| Models | `generator` | RUNTIME_SAFE | TENANT | Yes |
| Models | `verifier` | RUNTIME_SAFE | TENANT | Yes |
| Models | `credentials.openai` | SECRET_MANAGED_EXTERNALLY | SYSTEM | No |
| Models | `credentials.anthropic` | SECRET_MANAGED_EXTERNALLY | SYSTEM | No |
| Grounding | `sufficiency.ordinary.min_supporting_blocks` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.ordinary.min_independent_sources` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.ordinary.require_non_assessment_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.ordinary.require_table_structure` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.ordinary.require_formula_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.ordinary.require_visual_interpretation` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.table.min_supporting_blocks` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.table.min_independent_sources` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.table.require_non_assessment_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.table.require_table_structure` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.table.require_formula_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.table.require_visual_interpretation` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.formula.min_supporting_blocks` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.formula.min_independent_sources` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.formula.require_non_assessment_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.formula.require_table_structure` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.formula.require_formula_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.formula.require_visual_interpretation` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.figure.min_supporting_blocks` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.figure.min_independent_sources` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.figure.require_non_assessment_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.figure.require_table_structure` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.figure.require_formula_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.figure.require_visual_interpretation` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.assessment.min_supporting_blocks` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.assessment.min_independent_sources` | RUNTIME_SAFE | TENANT | Yes |
| Grounding | `sufficiency.assessment.require_non_assessment_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.assessment.require_table_structure` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.assessment.require_formula_source` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.assessment.require_visual_interpretation` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.retrieval_scores_permitted` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.vision_analysis_available` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.budget_omission_is_insufficient` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.incomplete_context_is_insufficient` | IMMUTABLE | TENANT | No |
| Grounding | `sufficiency.conflict_policy` | IMMUTABLE | TENANT | No |
| Grounding | `grounding.pretrained_knowledge_is_evidence` | IMMUTABLE | TENANT | No |
| Grounding | `grounding.corpus_access` | IMMUTABLE | TENANT | No |
| Grounding | `grounding.provider_tools_enabled` | IMMUTABLE | TENANT | No |
| Grounding | `grounding.provider_web_search_enabled` | IMMUTABLE | TENANT | No |
| Safety | `repair.enabled` | RUNTIME_SAFE | TENANT | Yes |
| Safety | `repair.max_attempts` | IMMUTABLE | TENANT | No |
| Safety | `repair.may_widen_evidence` | IMMUTABLE | TENANT | No |
| Safety | `ask.requires_verified_pass` | IMMUTABLE | TENANT | No |
| Safety | `ask.stream_answer_tokens` | IMMUTABLE | TENANT | No |
| Safety | `claim_verification.deterministic_checks_are_final` | IMMUTABLE | TENANT | No |
| Safety | `claim_verification.require_citation_for_material_claims` | IMMUTABLE | TENANT | No |
| Safety | `claim_verification.check_numeric_agreement` | IMMUTABLE | TENANT | No |
| Safety | `claim_verification.check_negation_agreement` | IMMUTABLE | TENANT | No |
| Safety | `claim_verification.check_certainty_overstatement` | IMMUTABLE | TENANT | No |
| Safety | `claim_verification.semantic_verification_enabled` | IMMUTABLE | TENANT | No |
| Safety | `claim_verification.verifier_required` | IMMUTABLE | TENANT | No |
| Safety | `final_verification.require_all_material_claims_supported` | IMMUTABLE | TENANT | No |
| Safety | `final_verification.require_deterministic_citation_success` | IMMUTABLE | TENANT | No |
| Safety | `final_verification.contradiction_abstains` | IMMUTABLE | TENANT | No |
| Safety | `final_verification.verifier_failure_abstains` | IMMUTABLE | TENANT | No |
| Safety | `final_verification.visual_claims_abstain` | IMMUTABLE | TENANT | No |
| Safety | `contradiction.assessment_cannot_override_reference` | IMMUTABLE | TENANT | No |
| Safety | `contradiction.rank_may_break_ties` | IMMUTABLE | TENANT | No |
| Safety | `contradiction.check_uncited_retained_evidence` | IMMUTABLE | TENANT | No |
| Safety | `contradiction.check_authoritative_disagreement` | IMMUTABLE | TENANT | No |
| Safety | `embedding.truncation_policy` | IMMUTABLE | SYSTEM | No |
| Ingestion | `ingestion.max_upload_bytes` | RESTART_REQUIRED | SYSTEM | No |
| Ingestion | `ingestion.max_retries` | RESTART_REQUIRED | SYSTEM | No |
| Ingestion | `ingestion.validation_timeout_seconds` | RESTART_REQUIRED | SYSTEM | No |
| Ingestion | `ingestion.upload_timeout_seconds` | RESTART_REQUIRED | SYSTEM | No |
| Ingestion | `sparse_index.verify_postings` | IMMUTABLE | SYSTEM | No |
| Observability | `service_name` | RESTART_REQUIRED | SYSTEM | No |
| Source Authority | `authority.GUIDELINE` | IMMUTABLE | TENANT | No |
| Source Authority | `authority.REFERENCE_BOOK` | IMMUTABLE | TENANT | No |
| Source Authority | `authority.TEXTBOOK` | IMMUTABLE | TENANT | No |
| Source Authority | `authority.COURSE_MATERIAL` | IMMUTABLE | TENANT | No |
| Source Authority | `authority.QUESTION_BANK` | IMMUTABLE | TENANT | No |
| Source Authority | `authority.QUESTION_PAPER` | IMMUTABLE | TENANT | No |
| Source Authority | `authority.ANSWER_KEY` | IMMUTABLE | TENANT | No |
| Source Authority | `authority.OTHER` | IMMUTABLE | TENANT | No |

## Provider registry and secrets

`MEDRAG_APPROVED_MODELS` is a startup JSON array of existing `ModelSelection` objects. Configured
generator and verifier pairs are also approved automatically. Only installed OpenAI/Anthropic
adapters are available; model IDs are never hardcoded in the frontend. Selection is runtime-safe
because a request captures both the selected identity and verification policy. Missing credentials
still fail closed; no provider or model fallback is introduced. A NULL verifier retains M8's explicit
generator fallback and its independence warning. A NULL generator leaves generation unavailable.

Keys are provisioned externally and delivered to the API only. Presence is a boolean, never a prefix,
suffix or length. No key is in the registry, response, snapshot, event or frontend bundle. Unknown
rejected keys/values and invalid request bodies are omitted from audit metadata. Reasons are bounded
and reject configured credentials and recognizable bearer/key shapes; do not use this field for
confidential content. Generic data-loss prevention of arbitrary user-entered prose is not claimed.

Source authority is read-only here and continues through the existing audited document metadata
workflow. Assessment sources cannot be promoted into reference truth by a policy toggle. M7
sufficiency remains structural. M8 verification, contradiction checks, citation requirements and
M9 verified-only delivery remain mandatory. Scores never become confidence thresholds.

## API and concurrency

- `GET /api/v1/settings` and `/effective`: typed metadata plus current revision and fingerprint.
- `POST /api/v1/settings/preview`: expected_revision and bounded registered changes; no mutation.
- `POST /api/v1/settings/changes`: same values, preview_token, confirmed=true and optional reason.
- `GET /api/v1/settings/history?limit=20&offset=0`: immutable tenant history, bounded pagination.

Every mutation locks the existing tenant row, including the first-writer case. Expected revision
rejects stale requests (409). A SHA-256 preview digest binds tenant, startup fingerprint, old/new
values and impact; a changed value or deployment requires another preview. Confirmation is enforced
server-side. Revision row and success audit event commit together; rejection audits contain only a
declared failure code. Historical revisions have no UPDATE/DELETE API, and a PostgreSQL trigger
rejects direct row updates/deletes. Database privileges remain those of the development deployment;
this is not protection against a database superuser changing its schema.

`configuration_changes_total{result=ACTIVE|PENDING_REBUILD|REJECTED}` is reconstructed from durable
audit events; `configuration_pending_rebuild` counts desired keys in latest tenant revisions. Labels
contain neither user IDs, arbitrary settings values, source text nor provider material. Mandatory
audit events have no disable switch. Security and metric collection continue from existing services.

## Persistence and operations

`m10_configuration` adds `configuration_revisions` and nullable
`conversation_turns.configuration_snapshot`. Apply migrations before starting the M10 API image.
Existing .env configuration and optional-provider startup remain valid. Rebuild API/frontend images
after source changes; worker/retrieval remain separate services. No remote Docker control is exposed.

Downgrade refuses populated configuration history or turn snapshots. Only after an explicit decision
to lose that history may an operator use MEDRAG_ALLOW_CONFIGURATION_LOSS=1 with Alembic downgrade.
Never set it during ordinary deployment. The integration harness sets it only for its UUID-scoped
throwaway schema. Rollback to M9 also stops consuming M10 tenant overrides; plan that effective-policy
change explicitly even when retaining the additive database schema.

## Limits

No provider availability probing, model-quality approval workflow, restart orchestration, index
rebuild orchestration, pending-proposal fulfillment, global admin identity or generic environment
editor is added. Synthetic tests demonstrate policy plumbing and safety contracts, not clinical
accuracy. Settings changes are not automatically quality improvements; evaluate substantive policy
changes with the established corpus-specific runners. M11 and M12 are not implemented by M10.
