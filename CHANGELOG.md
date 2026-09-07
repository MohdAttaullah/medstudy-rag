# Changelog

## M10 - 2026-09-07

- Added authorized, versioned tenant configuration: an explicit typed registry of 125 settings
  across ten sections, projected from the existing M3-M9 policies rather than serialized from
  Settings, so no environment variable, endpoint or credential is editable through it.
- Gave every setting a backend-owned lifecycle and made the frontend render that classification
  instead of deriving its own. 43 settings are editable; the other 82 are read-only, and 61 of
  those are architectural invariants an administrator cannot weaken.
- Refused to make safety optional. There is no control anywhere to disable the sufficiency gate,
  disable claim verification, release unverified answers, stream unverified tokens, treat
  pretrained knowledge as evidence or search partial ingestion. The gate thresholds that are
  editable already sit at their floor, so every reachable change makes the gate stricter.
- Separated what a change activates from what it merely proposes. Runtime-safe changes apply to
  subsequent requests through a copied service graph; chunk and analyzer changes stay pending and
  never move an effective value, because this system does not rebuild an index to match a
  dropdown.
- Stored tenant policy as append-only revisions with a database trigger that rejects any mutation
  of history, optimistic revision checks, tenant row locking and preview digests, so two
  administrators cannot silently overwrite each other and a browser cannot preview one value and
  submit another under the old approval.
- Captured one immutable policy snapshot per request and stored it with the Ask turn, so a
  historical verified answer stays traceable to the configuration that produced it.
- Declined to let a configuration surface tighten the policy it exposes. A validation rule that
  looked reasonable in isolation would have rejected an evidence budget that M6 explicitly permits
  and tests; it was removed, while the two rules M5 genuinely declares were kept and documented as
  M5's own.
- Kept credentials out entirely: presence is shown, values never are, approved model pairs come
  only from server startup configuration, and a change reason that contains a credential is
  refused rather than written into audit history.

## M9 - 2026-09-07

- Added the public Ask endpoint and page: the first surface in this system that may show a medical
  answer, and it shows one only when M8 verified every material claim.
- Made the display rule unrepresentable to violate. The public response carries an answer if and
  only if its outcome is VERIFIED, a refusal carries no claims, citations or sources, and two
  database CHECK constraints enforce the same rule beneath the application — so a rejected draft
  cannot be placed in a response, a stored turn or a history view even by mistake.
- Distinguished five outcomes rather than one failure: verified, insufficient evidence, conflicting
  sources, unverified draft and technical failure. An outage is never reported as missing evidence,
  and missing evidence is never filled in from model knowledge.
- Kept every M5–M8 response exactly as it was, still refusing to answer. Enablement is a property of
  the new contract, not a global switch, and no configuration can relax the verified requirement or
  turn on draft-token streaming.
- Added tenant-scoped conversations with a real migration, storing the question, the outcome, the
  answer only when verified, and the citations behind it — and deliberately not storing prompts,
  provider responses, failed drafts, verifier reasoning or evidence sets.
- Stored citation text rather than re-resolving it, so a later re-parse cannot silently change what
  a stored answer appears to cite, and refused to downgrade the migration while question history
  exists unless the loss is explicitly acknowledged.
- Made asking a reading capability while keeping the inspector closed: a reader may ask a question
  and read their own conversations, and still cannot see a draft, a lane score or a verifier verdict.
- Resolved conversation ownership from the authenticated principal and before any provider call, so
  another tenant's id is indistinguishable from one that never existed and costs nothing.
- Rendered citations into the existing authorized parse viewer rather than adding document routes,
  linking to the page the cited span is actually on, highlighting only regions M2 really recorded,
  and labelling assessment material wherever it appears.
- Returned bounded progress stages while waiting and delivered answer text atomically after
  verification; unverified tokens are never streamed.
- Verified live end to end with the configured OpenAI provider: a synthetic document ingested
  through the real worker, retrieved, reranked, gated, drafted, verified and delivered as a verified
  answer whose citation resolved to three real source elements with a real bounding box.
- Fixed three defects found during the work: relaxing service-level authorization briefly opened the
  M5 search endpoint to readers, an unknown conversation id was detected only after a provider call,
  and a citation opened the citation's first page rather than the page its region is on.

## M8 - 2026-09-07

- Added claim-level verification between the M7 grounded draft and any released answer. A draft
  becomes an answer only when every material claim survives every check; every other path abstains.
- Extracted claims deterministically from the answer text a reader would see rather than from the
  claim list the generator declared, so a sentence the generator never bound to evidence is caught
  as uncited instead of shipping inside a verified answer.
- Split conjoined propositions apart, biased toward over-splitting, so a statement joining a
  supported claim to an unsupported one cannot pass on the strength of its first half.
- Ran deterministic checks before any model and made them binding: citation identity within the
  request's own EvidenceSet, provenance resolution, value-and-unit agreement, negation polarity,
  certainty overstatement, canonical table headers and formula artifacts. A model is never asked
  whether a nonexistent citation, a wrong dose or a reversed negation is acceptable.
- Added a provider-neutral claim verifier that sees one claim and only the evidence it cites, with
  no corpus, no retrieval, no web search and no rank or score, returning a strict verdict schema.
- Made generator/verifier independence configurable and reported it honestly, because a verifier
  that is the same model as the generator shares its blind spots.
- Extended contradiction detection to the case a generator actually creates: a claim supported by
  the source it cited and contradicted by a source retrieval retained but did not cite. Competing
  evidence is preserved, and rank never decides which source is true.
- Permitted exactly one repair, constrained to the same evidence and instructed that removing an
  unsupported statement is success, then re-verified the repaired draft in full. A second failure
  abstains; there is no repair loop.
- Confined `verified=true` to a single object built only behind a PASS, kept M7 drafts unverified,
  and left `answering_enabled` false — the user-facing Ask experience is still a later milestone.
- Retained every failed verdict in the request trace, so an abstention says what failed instead of
  quietly returning what remained.
- Measured, and reported as measurements: 19 verification cases with **zero false PASSes**, full
  agreement with the labelled outcomes, an abstention rate of 0.79, and a live OpenAI run in which
  the first draft failed, the single repair succeeded and the repaired draft was re-verified before
  release.
- Fixed three real defects the evaluation itself found: a numeric claim drawn from a table skipped
  the header check, negation checking fired on claims containing no negation, and one fixture did
  not encode the conflict it named.
- Added no schema change and no migration; the Alembic head remains `m5_hybrid_retrieval`.

## M7 - 2026-09-07

- Added the Evidence Sufficiency Gate between the M6 EvidenceSet and any provider call, emitting
  SUFFICIENT, INSUFFICIENT or CONFLICTING from structural properties of the evidence — supporting
  anchors, independent document versions, source authority, required artifact presence and
  completeness, budget omissions, partial fragments, retrieval warnings and detected conflicts.
- Kept every retrieval, BM25, dense, RRF and CrossEncoder score out of that decision, pinned false
  by type and asserted by test. M6's observation that the CrossEncoder logit separated its synthetic
  positives from its negatives was deliberately not turned into a threshold.
- Made requirements question-kind aware through deterministic classification with no model and no
  rewriting, so a table question needs its header rows, a formula question needs its formula, and a
  figure question abstains because no vision-analysis path is approved. The M6 table and figure
  context gaps now surface as abstentions rather than being reconstructed by a generator.
- Made assessment material never sufficient on its own: a question bank or answer key records what
  an examiner marked, not what the corpus establishes, and a high rerank position does not change
  that.
- Added narrow deterministic conflict detection for an assessment key the reference evidence does
  not support, and for independent sources stating different values for the same labelled quantity.
  Competing evidence is preserved and returned; nothing chooses between sources.
- Added provider-independent generation reached only after SUFFICIENT, with isolated OpenAI and
  Anthropic adapters over the existing httpx dependency, a deterministic double for tests, and a
  scan asserting no vendor endpoint or import exists outside the adapters.
- Restricted generator input to the grounding policy, the question and the EvidenceSet, with no
  corpus handle, no tool, no web search and no rank or score, and stated in the prompt that
  pretrained model knowledge is not valid evidence.
- Validated every citation against the evidence actually supplied, rejecting invented or foreign
  ids. This is a contract check: whether a cited block supports its sentence is M8.
- Made every declared generation failure abstain, with no fallback to an ungrounded answer and none
  to a different provider or model, so a draft's recorded provenance stays true.
- Kept provider keys backend-only, delivered by compose to the API alone, absent from responses,
  logs, metric labels and the frontend bundle, with VITE_-prefixed copies forbidden by test.
- Added an authorized inspector showing the sufficiency decision, its evaluated signals, the
  preserved conflicts and the grounded draft, labelled unverified and never as an answer.
- Measured, and reported as measurements: 14/14 agreement with zero false allows on a fixture
  written alongside the gate, an abstention rate of 0.71, and live abstentions on real uploads for
  incomplete table structure, unavailable visual interpretation and assessment-only evidence.
- Added no schema change and no migration; the Alembic head remains `m5_hybrid_retrieval`. Jobs
  still end at RETRIEVAL_READY, `READY` stays unreachable, `answering_enabled` stays false, and no
  claim verification exists.
- Verified the OpenAI adapter against the live API once, on synthetic non-sensitive evidence: the
  gate permitted generation, the model returned a schema-valid draft whose every citation named
  supplied evidence, and the result stayed `verified=false` and awaiting claim verification.
- Stopped sending `temperature` unless it is explicitly configured, after the live call showed that
  a model may reject any explicit value outright, and recorded the temperature actually used —
  `None` for the provider default — so a draft's provenance states what really reached the provider.
- Separated a rejected provider request from an outage as `GENERATION_PROVIDER_REJECTED_REQUEST`,
  carrying only the provider's machine-readable code and parameter, never its prose.
- Fixed a pre-existing defect where the `MEDRAG_RERANKER__OFFLINE=true` value documented in
  `.env.example` could not be loaded at all, breaking every `Settings()` construction once it was
  copied into `.env`. The string form is accepted and the local-only pin still holds.

## M6 - 2026-09-07

- Added MedCPT CrossEncoder reranking of the fused M5 candidate pool, with the model and tokenizer
  pinned by revision and all seven checkpoint files SHA-256 verified before an offline, restricted
  load. Raw float32 logits rank candidates descending, ties break on the original fused rank then
  chunk UUID, and an over-long query/passage pair is rejected rather than truncated.
- Reranked over the full persisted M3 retrieval text, never the API preview, and kept the raw score
  labelled a ranking diagnostic everywhere it is exposed. No sigmoid, no confidence, no threshold.
- Added deterministic context expansion — bounded parent text for incomplete fragments, at most one
  sibling each side within the same parent and run, and canonical table, formula, figure and
  question artifacts — with no model deciding what context means.
- Added a request-scoped EvidenceSet: token, block and per-block budgets, atomic units omitted whole
  with an explicit finding rather than split, exact-identity deduplication that preserves distinct
  documents, versions and conflicting values, and no semantic-similarity merging.
- Fixed two provenance defects: an original figure with no caption is no longer dropped for having
  no text representation, and an atomic question assembled from several chunks now reports every
  contributing chunk instead of only its anchor. The Evidence Inspector links each of them.
- Added an authorized `/retrieval/rerank` endpoint that preserves the M5 diagnostics, rechecks
  corpus identity after retrieval, after inference and after expansion hydration, and fails closed
  on drift rather than returning a mixed evidence set.
- Kept M5 unchanged: the Query Encoder, analyzer, BM25 parameters, RRF equation and constant, lane
  weights and lane budgets are untouched, and the M6 candidate pool is a separately versioned
  query-side policy.
- Measured, and reported as measurements: on the synthetic fixture reranking moves nDCG@5 from
  0.9809 to 0.9967 and Recall@3 from 0.9773 to 1.0000 while Recall@1 stays at 0.7500, with zero
  first-stage misses and zero reranker regressions; one sibling each side raises context coverage
  from 0.8833 to 0.9333 with no measured increase in noise; and table and figure context gaps
  remain open at every budget.
- Measured that the raw CrossEncoder logit separates answerable from unanswerable synthetic queries
  where the fused RRF score does not — recorded as an observation, deliberately not turned into a
  sufficiency threshold.
- Added no schema change and no migration; the Alembic head remains `m5_hybrid_retrieval`. Jobs
  still end at RETRIEVAL_READY, `READY` remains unreachable and `answering_enabled` stays false.

## M5 - 2026-09-06

- Added query-side retrieval with the MedCPT Query Encoder pinned by revision and by both weight
  and tokenizer checksum, reproducing the released representation: CLS pooling, 768 dimensions,
  unnormalized, 64-token maximum, inner-product similarity against the M4 article vectors.
- Added a versioned biomedical BM25 lane in PostgreSQL whose analyzer keeps identifiers intact —
  HLA-B27, CYP3A4, Na+/K+-ATPase, HbA1c, IL-6, mg/kg, 7.5% — with stopwords disabled and no
  synonym or abbreviation expansion, enforced by a database constraint.
- Stored raw term frequencies and document lengths rather than pre-weighted scores, so BM25 k1 and
  b are runtime-safe and inverse document frequency is scoped to the tenant's own active corpus.
- Added reciprocal rank fusion with deterministic ties, and DENSE_ONLY, BM25_ONLY and HYBRID_RRF
  modes. Raw lane scores are carried as diagnostics and never added together.
- Made dense/lexical corpus alignment fail closed: a version is searchable only when both lanes are
  active, verified and built from the same chunk dataset.
- Added a durable sparse-index lifecycle with reconciliation against the dense lane's own recorded
  chunk set, activation only after verification, and preservation of the previous active index when
  a replacement fails.
- Rejected over-long queries with a structured error instead of truncating them, and kept query
  text out of logs, traces and the bounded query-vector cache.
- Moved interactive query encoding into its own internal service with an offline provisioned model
  cache, so the API and dispatcher images carry no torch and no request downloads a model.
- Added a retrieval API, sparse-index inspection, an explicit lexical rebuild, and a Retrieval
  Inspector UI with lane comparison, execution trace and candidate to source-page navigation.
- Added a gold retrieval dataset and an evaluation harness reporting Recall@K, MRR, nDCG, precision,
  per-category results, per-query diagnostics, failure classification, negative-case score
  separation and query-boundary behaviour, with the dataset hash recorded in every report.
- Measured, and reported as measurements: the fused RRF score does not separate answerable from
  unanswerable queries and must not be used as an evidence-sufficiency signal.
- Advanced the pipeline to RETRIEVAL_READY. Ask remains disabled, READY remains unreachable, every
  version remains database-constrained unsearchable, and every retrieval response states
  answering_enabled: false. No reranking, expansion, grounding or generation exists.

## M4 - 2026-09-06

- Added document-side embeddings with the MedCPT Article Encoder pinned by revision and weight
  checksum, reproducing the released representation: CLS pooling, 768 dimensions, unnormalized,
  512-token maximum, inner-product similarity.
- Added a provider-independent embedding abstraction; only one adapter imports transformers or
  torch, and only one imports the vector-database client.
- Added deterministic two-field embedding inputs built solely from persisted chunk content and
  declared hierarchy, with a SHA-256 hash over exactly what the model sees.
- Made truncation impossible to apply silently: an over-long chunk fails the run with a finding
  naming it, and a truncated vector is unstorable by database constraint.
- Excluded parent chunks from first-stage retrieval by policy, and kept question-bank material
  distinguishable through payload authority metadata.
- Added durable EmbeddingVersion, EmbeddingRun, ChunkEmbedding, IndexRun and IndexValidationFinding
  records, with one active embedding run and one active index run per version enforced by the
  database.
- Added a Qdrant index with named dense vectors, deterministic uuid5 point identity, provenance-only
  payloads, tenant-oriented payload indexing and server-side tenant scoping.
- Added staged indexing with point-for-point read-back reconciliation, and activation as a
  PostgreSQL state change that happens only after verification, so a failed replacement leaves the
  previous active index untouched.
- Made superseding a chunk dataset automatically deactivate the embeddings and index built from it.
- Executed READY_FOR_EMBEDDING -> EMBEDDING -> INDEXING -> VERIFYING_INDEX -> READY_FOR_RETRIEVAL in
  the worker with idempotent delivery, cancellation, lease sweeping and an explicit permissioned
  re-embed.
- Added offline model provisioning with checksum verification and an `embedding-models` volume, so
  no user request depends on a runtime model download.
- Added tenant-authorized embedding and index inspection APIs, an embedding summary on document
  details, an Index Inspector, and the real index stages in Operations. No dense vector is exposed.
- Added the offline embedding technical-quality evaluation harness and a host/container numerical
  comparison tool.
- Indexing stops at READY_FOR_RETRIEVAL. Query retrieval, reranking and answering stay disabled,
  and READY remains unreachable.

## M3 - 2026-09-06

- Added structure-aware hierarchical chunking over the active parse run: parent and child chunks,
  row-grouped table parts with identical repeated headers, atomic formulas, figure context chunks
  and atomic question objects with their options.
- Added a durable versioned ChunkRun with chunker/policy identity, a policy fingerprint, a separate
  normalized-input fingerprint, a fenced lease and a single active dataset enforced by the database.
- Bundled the MedCPT WordPiece tokenizer with its revision, file checksum and runtime pinned, used
  only to measure and slice tokens offline; a mismatch fails closed.
- Added complete span-level provenance: every chunk resolves to source elements, offsets, pages,
  artifacts and the active parse run, and the UI links back to the exact source page.
- Preserved explicit source answers and left absent answers absent; an inferred answer is
  unstorable by database constraint.
- Added a deterministic chunk-quality layer with persisted findings, PASS/PASS_WITH_WARNINGS/
  NEEDS_REVIEW/FAIL, and source coverage measured by text rather than by element identity.
- Executed READY_FOR_CHUNKING -> CHUNKING -> VALIDATING_CHUNKS -> READY_FOR_EMBEDDING in the worker
  with idempotent delivery, cancellation, lease sweeping and an explicit permissioned rechunk.
- Made completed chunk datasets immutable and made superseding a parse run or cancelling a job
  deactivate the datasets built from it.
- Added tenant-authorized chunk inspection APIs, a chunk summary on document details, a Chunk
  Inspector with question and table views, and the real chunk stages in Operations.
- Added the offline chunk construction evaluation harness over 14 synthetic normalized fixtures.
- Chunking stops at READY_FOR_EMBEDDING. Embeddings, indexing, retrieval and answering stay
  disabled, and their job states remain unreachable.

## M2 - 2026-09-06

- Added Docling parsing behind a parser-independent abstraction; only one adapter imports Docling.
- Added durable versioned ParseRun with parser/policy identity, content fingerprint, source
  checksum, lease and a single active dataset enforced by the database.
- Persisted the raw parser artifact, page previews and figure crops in private object storage.
- Added parser-independent pages, elements, tables, figures and formulas with 1-based page
  numbers, TOPLEFT point coordinates, parser-declared hierarchy and deterministic reading order.
- Added deterministic normalization that repairs extraction artefacts and never rewrites content.
- Added OCR configuration with recorded engine, per-page OCR indicator and suspicious-OCR review.
- Added a deterministic parse-quality layer with persisted findings and PASS/PASS_WITH_WARNINGS/
  NEEDS_REVIEW/FAIL, plus fail-closed parse errors with declared retryability.
- Executed QUEUED -> PARSING -> NORMALIZING -> ENRICHING -> READY_FOR_CHUNKING in the worker, with
  idempotent delivery, cancellation handling, lease reaping and an explicit reparse action.
- Added tenant-authorized parse inspection APIs, a parse summary on document details, a parse
  inspector with page previews and table/figure/formula views, and real stages in Operations.
- Added the parsing extraction-fidelity gold dataset and evaluation harness.
- Parsing stops at READY_FOR_CHUNKING. Chunking, embeddings, retrieval and answering stay disabled.

## M1 - 2026-09-05

- Added development authentication and server-enforced tenant/role permissions.
- Added PDF upload validation, immutable versioned originals, metadata, SHA-256 duplicates,
  durable upload-intent recovery and transactional ingestion outbox.
- Added PostgreSQL migrations, guarded job transitions and append-only ordered history/audit.
- Added real Celery receipt, bounded retries/cancel/archive, config snapshots and safe telemetry.
- Connected Library uploads/details and Operations to persisted APIs with real transfer progress.
- Added synthetic fixtures, failure/race/security tests and a live upload/worker browser flow.
- Successful processing stops at QUEUED. Medical answering and M2 processing remain disabled.

## 0.1.0 — 2026-09-05

- Added M0 monorepo foundation, original agent instructions and synchronized project skills.
- Documented target architecture with five ADRs and explicitly synthetic evaluation fixtures.
- Added typed configuration, FastAPI liveness/readiness, correlation IDs, safe request logging and
  request metrics; SQLAlchemy/Alembic and provider/Celery interface foundations.
- Added React workspace routes with disabled medical answering and live Operations health.
- Added Compose definitions, optional app/worker containers, dependency locks and verification commands.
- No domain ingestion, retrieval, generation, RBAC or production deployment is implemented in M0.
