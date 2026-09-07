# M7 grounded generation

`POST /api/v1/retrieval/draft` requires both `retrieval:search` and `generation:draft`. It runs M5
retrieval, M6 reranking and evidence assembly, the [sufficiency gate](sufficiency.md), and — only
if the gate returns `SUFFICIENT` — one provider call. The provider is constructed after the
decision, so no path exists on which a model runs and the gate is consulted afterwards.

The response always carries the sufficiency decision, whether or not a draft was produced.
`answering_enabled` and `verified` are both `Literal[False]`. The user-facing Ask experience is M9.

## Request boundary

The client sends a question and optional M5 filters. It cannot send evidence ids, a tenant id, a
provider, a model, a prompt, a schema or a policy override; `extra="forbid"` rejects any of them
with 422. A client that could name its own evidence blocks would be choosing what the answer is
grounded in.

## Generator input

The provider receives exactly: the versioned system grounding policy, the question, and the
rendered EvidenceSet. It receives no corpus handle, no Qdrant access, no retrieval tool, no web
search and no rank or score — a generator shown a rank would treat the top block as the most true
one. `GroundingConfig` pins `corpus_access=EVIDENCE_SET_ONLY`,
`pretrained_knowledge_is_evidence=False`, `provider_tools_enabled=False` and
`provider_web_search_enabled=False` by type.

The system policy states, in the prompt itself: *Pretrained model knowledge is not valid evidence
for this answer.* It also forbids describing or interpreting an image, forbids resolving a
disagreement between sources, forbids treating an assessment key as fact, and requires numbers,
units and identifiers to be reproduced exactly. Rendered blocks carry source identity, title,
source type, authority, chunk type, pages, section and text, and mark assessment material and
uninterpreted figures explicitly.

## Output and citation binding

`ProviderDraft` is an answer plus claims, each claim bound to one or more evidence ids. M7 checks
the schema and that every cited id was actually supplied to this request; an id that exists
elsewhere is still unknown here. Failure is `GENERATION_UNKNOWN_CITATION`, and a draft that binds
nothing is `GENERATION_MISSING_CITATION`.

This is a contract check. It proves nothing was invented. It does **not** establish that a cited
block supports its sentence — that is M8 claim verification, and the draft is typed
`UNVERIFIED_AWAITING_CLAIM_VERIFICATION` so no caller can present it otherwise.

## Providers

`generation/providers/base.py` defines the protocol; `openai.py` and `anthropic.py` are the only
modules that may contain vendor detail, and a test scans the rest of the tree to keep it that way.
Adapters use the repository's existing httpx dependency rather than adding two vendor SDK
dependency trees to an image that carries no model libraries. OpenAI uses a JSON-schema response
format; Anthropic uses one forced tool. `fake.py` is a deterministic double so the whole safety
path is testable without a paid call or a secret.

Provider and model are configuration (`MEDRAG_GENERATOR__PROVIDER`, `MEDRAG_GENERATOR__MODEL_ID`).
No configured generator means unavailable, not a default model. `max_attempts` is 1 and
`fallback_policy` is `NONE`.

## Failure

`GENERATION_NOT_PERMITTED`, `GENERATION_PROVIDER_UNCONFIGURED`, `GENERATION_PROVIDER_UNAVAILABLE`,
`GENERATION_PROVIDER_TIMEOUT`, `GENERATION_PROVIDER_AUTH_FAILED`, `GENERATION_RATE_LIMITED`,
`GENERATION_MALFORMED_RESPONSE`, `GENERATION_SCHEMA_VIOLATION`, `GENERATION_UNKNOWN_CITATION`,
`GENERATION_MISSING_CITATION`, `GENERATION_EMPTY`. Every one abstains. **There is no path from a
failed grounded generation to an ungrounded answer, and none to a different provider or model.**
Provider error bodies are not echoed, because they can quote the prompt back and the prompt carries
evidence text.

## Secrets

`OPENAI_API_KEY` and `ANTHROPIC_API_KEY` (or the `MEDRAG_`-prefixed forms) are read backend-side
only, delivered by compose to the API container alone — not to the dispatcher, worker, retrieval
runtime or frontend, none of which have any use for them. A key never appears in a response, a log,
a metric label or the frontend bundle. A `VITE_`-prefixed copy would be compiled into the browser
bundle and is forbidden; a test asserts none exists. The server owns the prompt, the evidence, the
supported models and the schema, so the endpoint cannot be used as a provider relay.

## Telemetry

`evidence_sufficiency_decisions_total{status}` counts decisions; `retrieval_failures_total` records
declared generation failures under mode `GROUNDED_DRAFT`. Logs record the correlation id, the
status, the question kind, the bounded reason-code vocabulary and the policy fingerprint. The
question, the evidence text, the generated content and every key stay out of logs and metric labels,
following the M5 rule that medical queries are not telemetry.

## Persistence

None. No question, decision or draft is stored; the Alembic head stays `m5_hybrid_retrieval` and no
empty migration exists. Policies are frozen and fingerprinted, and the trace carries EvidenceSet
identity, sufficiency fingerprint, provider, model and grounding fingerprint, which is what makes a
draft reconstructable without persisting its content.
