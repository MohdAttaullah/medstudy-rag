# ADR-012: The evidence gate decides, and only then may a provider write

Status: Accepted for M7. Implements the ADR-004 safety contract and the ADR-005 provider boundary. Changes no stored schema, no ingestion state and no M5/M6 retrieval semantics.

## Ordering

A question runs M5 hybrid retrieval, M6 reranking and evidence assembly, then the Evidence Sufficiency Gate, and only if the gate returns `SUFFICIENT` is a provider constructed and called. The provider is built after the decision, not before, so there is no code path on which a model runs and the gate is consulted afterwards. `services/generation.py` holds this ordering and nothing else may call a provider.

## No score is sufficiency

M6 measured that the CrossEncoder raw logit separated its 24 synthetic positives from its negatives. That is an engineering observation on a tiny fixture, not a calibration, and turning it into `if score > x` would have made an uncalibrated number the arbiter of whether a medical answer is allowed. `SufficiencyConfig.retrieval_scores_permitted` is `Literal[False]`, no policy field is a float, and no retrieval, BM25, dense, RRF or reranker score is reachable from the gate. A test asserts all three.

The decision is instead read from the structure of the EvidenceSet: how many ranked anchors support the question, how many independent document versions they come from, whether any non-assessment source is present, whether the artifact the question depends on is present and complete, whether anything was omitted for budget, whether any block is a partial fragment, and whether the sources disagree. Every one of those is reported as an `EvaluatedSignal` with the requirement it was compared against, so a decision can be re-read rather than trusted.

## Question kinds

Requirements differ by what the question needs, so the gate classifies it deterministically from a fixed cue table over the user's own words, falling back to the retrieved anchors' chunk types. There is no model, no rewriting and no paraphrase in that path. Classification only ever raises what the evidence must contain, so a misclassification abstains rather than answering from less. The tokenization is the M5 biomedical analyzer with a locally-declared config, deliberately not the tenant's active index policy: rebuilding a lexical index must not change what the gate decides.

A table question whose table part arrives without its header rows is `INSUFFICIENT`. A figure question is `INSUFFICIENT` in every case, because M7 approves no vision-analysis path and a caption — or the structural label M3 writes for a captionless figure — records that a figure exists rather than what it shows. Both adapters refuse `analyze_image` so the capability cannot be reached by accident. These are the M6 table and figure context gaps surfacing as abstentions instead of being papered over by a generator willing to reconstruct them.

## Conflict

M7 detects disagreement already present in the evidence; it does not analyse claims in a draft. Two deterministic detectors: an assessment key whose content the reference evidence in the same set does not support, and two independent non-assessment document versions stating different values for the same labelled quantity. Both over-trigger by design. An unnecessary abstention costs coverage; a silently resolved conflict emits a medical answer that the corpus does not support. Detected conflicts make the status `CONFLICTING` and every competing block is preserved and returned. Nothing chooses between sources, and rank never breaks the tie.

Status precedence is `CONFLICTING` over `INSUFFICIENT` over `SUFFICIENT`, because a disagreement is the more specific finding. Every triggered reason code from both evaluations is reported regardless, so the precedence hides nothing.

## Generation boundary

The provider receives the grounding policy, the question, and the rendered EvidenceSet. It receives no corpus handle, no Qdrant access, no retrieval tool, no web search, and no rank or score — a generator shown a rank would treat the top block as the most true one. The system policy states in the prompt that pretrained model knowledge is not valid evidence, and the same prohibition is pinned by type in `GroundingConfig`. The client sends a question and optional filters and cannot name evidence, tenant, provider, model, prompt or policy; `extra="forbid"` rejects any attempt.

Output is a structured `ProviderDraft` with an answer and claims, each claim bound to evidence ids. M7 validates the schema and that every cited id was actually supplied — a contract check that proves nothing was invented. It does **not** check that a cited block supports its sentence. That is M8 claim verification, and the draft is typed `UNVERIFIED_AWAITING_CLAIM_VERIFICATION` so no caller can present it otherwise. `answering_enabled` and `verified` are both `Literal[False]`; the user-facing Ask experience is M9.

## Providers

Adapters talk the OpenAI and Anthropic wire formats over the httpx client the repository already depends on, rather than adding two vendor SDK dependency trees to an image that deliberately carries no model libraries. ADR-005's requirement is that vendor detail stay inside adapters, and it does: base URLs, headers, wire versions, tool names and response shapes all live in `generation/providers/`, enforced by a test that scans the rest of the tree. Anthropic structured output uses one forced tool; OpenAI uses a JSON schema response format.

Provider and model are configuration. No configured generator means unavailable, not a default model, because an invented model identity would make a draft's recorded provenance a fiction. `max_attempts` is 1 and `fallback_policy` is `NONE`: a retry that silently reached a different provider would make the same record untrue. Every declared failure — timeout, auth, rate limit, malformed output, schema violation, unknown citation, empty response — abstains. There is no path from a failed grounded generation to an ungrounded answer.

Keys are backend-only, read from `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` (or the `MEDRAG_` prefixed forms), delivered by compose only to the API container, and never placed in a response, a log, a metric label or the frontend bundle. A `VITE_`-prefixed copy would be compiled into the browser bundle and is forbidden; a test asserts none exists.

## Persistence

Nothing about a question, a decision or a draft is stored. The Alembic head stays `m5_hybrid_retrieval` and no empty migration is created for milestone numbering. Policies are frozen and fingerprinted, and the trace carries the EvidenceSet identity, the sufficiency policy fingerprint, the decision, the provider, the model and the grounding policy fingerprint, which is what makes a draft reconstructable without persisting its content.

## Consequences

Coverage falls: on the M7 fixture the gate abstains on ten of fourteen cases, and every figure question abstains outright. That is the intended trade. The conflict detectors will produce false conflicts on text where a shared label and unit coincide, which costs further coverage and is recorded as a limitation rather than tuned away. None of the thresholds in the default requirements are calibrated; they are conservative starting points, and the fixture that measures them was written alongside them, which is stated wherever the numbers appear.
