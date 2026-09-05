# ENTERPRISE MEDICAL RAG PLATFORM — PROJECT BOOTSTRAP

You are the principal architect and senior implementation engineer for this repository.

Build an enterprise-grade, accuracy-first medical Retrieval-Augmented Generation platform for educational medical knowledge.

This is NOT a simple chatbot prototype.

The corpus will contain:

* medical textbooks
* medical reference books
* question banks
* previous question papers
* answer keys
* tables
* formulas
* figures
* diagrams
* scanned pages
* mixed text/image PDFs

The defining product requirement is:

> A wrong unsupported answer is worse than no answer.

The system must therefore be designed around grounded evidence, retrieval quality, provenance, verification and abstention rather than merely generating fluent responses.

---

# 1. NON-NEGOTIABLE PRINCIPLES

The following requirements override convenience.

1. The LLM must never be treated as the authoritative medical knowledge store.

2. Answers must be generated only from evidence retrieved from the indexed corpus.

3. If sufficient evidence is not available, the application must abstain.

4. Every substantive answer claim must be traceable to one or more source chunks.

5. Each citation must retain:

   * document
   * document version
   * edition if available
   * chapter
   * section
   * page
   * chunk ID
   * original page provenance
   * bounding box where available

6. The original document/image is the source of truth.

7. A generated caption, summary, OCR correction, query rewrite, or LLM interpretation is NEVER itself authoritative evidence.

8. Question-bank answer keys must not automatically override textbooks or higher-authority sources.

9. Conflicting high-quality evidence must trigger conflict handling rather than arbitrary answer selection.

10. Partially ingested documents must never become searchable.

11. Retrieval and safety quality must be measurable using automated evaluation.

12. Important architecture and behavior must not depend on a single LLM vendor.

13. Model providers must be abstracted behind interfaces.

14. Do not hard-code secrets, model IDs, thresholds or environment-specific infrastructure.

15. This application is initially an educational medical knowledge system, not a patient diagnosis/treatment product.

---

# 2. TARGET ARCHITECTURE

Use a modular monorepo.

Preferred technology stack:

Frontend:

* React
* TypeScript
* Vite
* Tailwind CSS
* TanStack Query
* accessible component architecture

Backend:

* Python
* FastAPI
* Pydantic v2
* SQLAlchemy 2
* Alembic

Relational metadata:

* PostgreSQL

Vector/search:

* Qdrant

Object storage:

* S3-compatible abstraction
* MinIO locally
* cloud S3-compatible provider in production

Queue:

* Redis
* Celery

Document intelligence:

* Docling

Biomedical dense retrieval:

* MedCPT Query Encoder
* MedCPT Article Encoder

Alternative/benchmark retriever:

* BGE-M3

Lexical:

* BM25 / sparse retrieval

Late interaction:

* optional ColBERT lane

Reranking:

* MedCPT Cross-Encoder

Generation:
Provider abstraction supporting at minimum:

* OpenAI
* Anthropic

Default configuration should select the strongest configured production model available but the architecture must not assume that a particular model exists.

Vision:
Use a strong multimodal provider model only when retrieved evidence actually includes a figure, image, diagnostic diagram or visually dependent content.

Observability:

* structured logging
* OpenTelemetry
* Prometheus-compatible metrics
* trace IDs
* ingestion correlation IDs

Testing:

* pytest
* frontend unit tests
* integration tests
* end-to-end tests
* retrieval evaluation tests

Local deployment:

* Docker Compose

Production deployment:

* containers
* Kubernetes-ready configuration
* stateless API services
* separately scalable worker pools

---

# 3. REQUIRED REPOSITORY STRUCTURE

Create a clean structure approximately like:

/
AGENTS.md
CLAUDE.md
AI_HANDOFF.md
README.md
CHANGELOG.md

docs/
architecture/
system-overview.md
ingestion.md
retrieval.md
grounding.md
data-model.md
security.md
deployment.md
observability.md

```
adr/
  ADR-001-document-parser.md
  ADR-002-vector-database.md
  ADR-003-retrieval-strategy.md
  ADR-004-grounding-policy.md
  ADR-005-model-provider-abstraction.md

evals/
  retrieval-evaluation.md
  answer-evaluation.md
```

backend/
app/
api/
core/
db/
models/
schemas/
services/
repositories/

```
  ingestion/
    parser/
    normalizer/
    chunker/
    tables/
    figures/
    formulas/
    qbank/
    embeddings/
    indexer/
    validation/

  retrieval/
    query/
    dense/
    sparse/
    late_interaction/
    fusion/
    reranking/
    expansion/

  generation/
    prompts/
    providers/
    grounding/
    verification/
    citations/
    abstention/

  evaluation/
  security/
  observability/

tests/
```

frontend/
src/
pages/
components/
features/
ask/
library/
documents/
settings/
evaluations/
operations/
audit/
api/
hooks/
types/

workers/
ingestion/
embedding/
validation/

migrations/

infrastructure/
docker/
kubernetes/
monitoring/

scripts/

.agents/
skills/

.claude/
skills/

---

# 4. DOCUMENT INGESTION STATE MACHINE

Implement ingestion as a durable, idempotent state machine.

Required states:

UPLOADED
VALIDATING
QUEUED
PARSING
NORMALIZING
CHUNKING
ENRICHING
EMBEDDING
INDEXING
VERIFYING_INDEX
READY

Failure states:

FAILED
QUARANTINED
NEEDS_REVIEW
CANCELLED

Requirements:

* Every uploaded file receives a document ID and document-version ID.
* SHA-256 file hashes are stored.
* Duplicate uploads must be detectable.
* Jobs must be retryable and idempotent.
* Each processing stage must record:

  * started_at
  * completed_at
  * status
  * retry count
  * worker
  * error type
  * sanitized error message
  * configuration version
  * model version where relevant

Never expose an incomplete index.

Use staged index activation.

A document version becomes searchable only after:

* parse succeeded
* chunk validation succeeded
* embeddings completed
* Qdrant records reconciled
* metadata reconciliation succeeded
* retrieval smoke tests succeeded

---

# 5. DOCLING DOCUMENT MODEL

Do not flatten parsed PDFs prematurely.

Preserve the DoclingDocument and structured JSON artifact.

Retain:

document
chapter
section
subsection
paragraph
list
table
formula
figure
caption
question
answer options
answer
explanation
page
bounding box
reading order

Store the original PDF permanently according to retention policy.

Store page images/crops when needed for provenance.

---

# 6. CHUNKING

Implement structure-aware hierarchical parent/child chunking.

Never use RecursiveCharacterTextSplitter as the main medical chunker.

Implement configurable child and parent chunks.

Initial values may start approximately around:

child target: 300–450 tokens
parent target: 1000–1600 tokens

but treat these strictly as benchmark starting values.

Chunk boundaries must respect:

* headings
* paragraphs
* lists
* tables
* formulas
* figure/caption relationships
* question/option/answer/explanation blocks

Tables:

* preserve headers
* keep small tables atomic
* split oversized tables by logical row groups
* repeat table headers after splitting

Formulas:

* formula expression and variable definitions must remain associated
* retain explanatory context

Question banks:
Represent each question as a logical domain object containing:

* question text
* options
* official answer if present
* explanation
* subject/topic
* source
* edition/year
* page
* authority level

Never separate question text from its choices or answer through arbitrary chunk boundaries.

---

# 7. RETRIEVAL

Implement multi-lane retrieval.

Lane A:
MedCPT dense retrieval.

Use the query encoder for user queries and article/document encoder for chunks.

Lane B:
BM25/sparse retrieval for exact biomedical terminology.

Lane C:
Optional ColBERT-style late interaction.

Do not assume Lane C should remain permanently enabled.
Its value must be demonstrated with evals.

Fuse first-stage candidate lists using Reciprocal Rank Fusion or another explicitly benchmarked method.

Retrieve broadly.

Example initial candidate configuration:

dense top K: 40
sparse top K: 40

Then rerank candidates with MedCPT Cross-Encoder.

Return approximately 5–8 strongest final evidence blocks by default.

All values must be configuration-driven.

---

# 8. CONTEXT EXPANSION

Retrieval chunks may be small for precision.

Generation context may require broader semantic context.

After final child chunks are selected:

* retrieve parent section where useful
* retrieve configurable neighbour window
* retrieve associated table/figure/formula
* deduplicate overlapping content
* obey context budget

Do not blindly append adjacent chunks.

Context expansion must remain provenance-preserving.

---

# 9. SOURCE AUTHORITY AND CONFLICTS

Every document must have metadata such as:

source_type
title
edition
publication_year
publisher
specialty
subject
authority_level

Support source types:

guideline
reference_book
textbook
course_material
question_bank
question_paper
answer_key
other

Implement configurable source precedence.

A question-bank key is assessment material, not automatically medical ground truth.

If evidence from sufficiently authoritative sources materially conflicts:

* mark retrieval result as conflicting
* do not silently choose one answer
* present the conflict or abstain according to policy

---

# 10. GENERATION

Implement provider abstraction, e.g.:

LLMProvider
generate()
generate_structured()
analyze_image()

OpenAIProvider
AnthropicProvider

Application code must not directly depend on OpenAI- or Anthropic-specific calls outside provider adapters.

Use structured outputs internally.

Generator input must include only:

* system grounding policy
* user question
* selected evidence
* citation IDs
* permitted response schema

The model must be told that its pretrained knowledge is not valid evidence.

Answer schema should include:

answer
claims[]
citations[]
confidence
evidence_status
conflicts[]
abstention_reason

Do not expose raw model confidence as a medical probability.

---

# 11. EVIDENCE SUFFICIENCY GATE

Before generation, determine whether retrieved evidence is sufficient.

Create a configurable evidence policy based on signals such as:

* reranker scores
* number of supporting chunks
* independent source count
* source authority
* conflict status
* retrieval coverage
* question type

Thresholds must be calibrated through evaluation.

Do not invent arbitrary "95% accuracy" numbers.

Possible outcome:

SUFFICIENT
INSUFFICIENT
CONFLICTING

Generation is allowed only for SUFFICIENT evidence.

---

# 12. CLAIM VERIFICATION

After generation:

1. Decompose answer into atomic factual claims.
2. Map each claim to its cited evidence.
3. Verify entailment/support.
4. Verify citation IDs exist.
5. Verify citation source/page metadata.
6. Reject unsupported claims.
7. If a material claim fails verification, regenerate once if policy permits.
8. If still unsupported, abstain.

Prefer using a verifier model different from the generator where configuration/cost permits, reducing correlated failures.

Also implement deterministic citation and provenance validation independent of the verifier LLM.

---

# 13. VISION

Vision models are helpers, not sources of truth.

For figures:

retain:

* original crop
* page number
* caption
* nearby text
* OCR text where applicable
* figure ID

A generated figure description may improve retrieval but must be marked GENERATED_METADATA.

If answering a question materially depends on interpreting the figure itself, send the original retrieved figure to the configured multimodal model during answer generation/verification.

Never replace the source image with an LLM-produced summary.

---

# 14. FRONTEND

Create professional enterprise UX.

Required pages:

/ask
/library
/documents/:id
/settings
/evaluations
/operations
/audit

ASK

Provide:

* conversation list
* question input
* streaming answer
* evidence status
* citations
* source panel
* page/figure preview
* abstention UI
* conflict UI
* retrieval debug view for authorized users

LIBRARY

Provide:

* drag/drop upload
* multi-file upload
* ingestion status
* progress
* errors
* retry
* cancel
* document metadata
* edition
* authority level
* source type
* reindex
* archive/delete

DOCUMENT DETAILS

Show:

* metadata
* versions
* ingestion stages
* page count
* extracted elements
* chunk count
* table count
* figure count
* formula count
* embedding/index version
* processing errors
* chunk inspector
* source page viewer

SETTINGS

Sections:

Chunking
Retrieval
Models
Grounding
Source authority
Ingestion
Safety
Observability

Settings must be schema-driven and validated.

Mark settings as:

* runtime-safe
* requires reindex
* requires service restart

Changing an embedding model or incompatible chunking configuration must create a new index/config version rather than silently mutating active data.

EVALUATIONS

Show:

* Recall@K
* MRR
* nDCG where applicable
* reranker metrics
* answer correctness
* citation correctness
* unsupported claim rate
* abstention precision/recall
* latency
* cost

OPERATIONS

Show:

* queue depth
* worker state
* ingestion jobs
* retries
* failed jobs
* latency
* model errors

---

# 15. DATABASE DOMAIN MODEL

Design normalized SQLAlchemy models for at least:

User
Role

Document
DocumentVersion
DocumentPage
DocumentElement
Chunk
ChunkRelation
TableArtifact
FigureArtifact
FormulaArtifact

IngestionJob
IngestionStage
ProcessingError

EmbeddingVersion
IndexVersion

RetrievalConfig
ModelConfig
GroundingConfig
SourceAuthorityConfig

Conversation
Question
Answer
AnswerClaim
Citation
EvidenceBundle

EvaluationDataset
EvaluationQuestion
EvaluationRun
EvaluationResult

AuditEvent

Use UUIDs.

Use timestamps.

Do not soft-delete accidentally where regulatory/audit semantics matter; explicitly model archival/deletion policy.

---

# 16. CONFIGURATION VERSIONING

Every answer should be reproducible.

Persist:

retrieval_config_version
embedding_version
index_version
generator model
verifier model
prompt version
source corpus version

This allows us to determine exactly how a historical answer was produced.

---

# 17. SECURITY

Implement enterprise-friendly architecture.

At minimum:

* RBAC
* secure password/auth abstraction
* OIDC-ready architecture
* secrets only via environment/secret managers
* input validation
* file-type allowlist
* upload size limits
* secure filenames
* authorization on every document endpoint
* audit logs
* no secrets in logs
* no raw provider API responses logged by default
* encryption-ready object storage
* encryption-ready database
* configurable retention

The initial product is educational.

Patient-specific medical diagnosis/advice should be outside initial scope and handled by an explicit safety policy.

Design PHI-aware boundaries but do not claim HIPAA compliance merely because security controls exist.

---

# 18. OBSERVABILITY

Every user query must receive a request/correlation ID.

Trace:

query
→ normalization
→ dense retrieval
→ sparse retrieval
→ fusion
→ reranker
→ expansion
→ evidence gate
→ generator
→ verifier
→ response

Record latency and failure state per component.

Do not log sensitive full prompts by default.

Support configurable debug tracing for non-sensitive development datasets.

---

# 19. EVALUATION-FIRST DEVELOPMENT

Do not wait until the end to create evals.

Build a small gold dataset containing representative:

* factual questions
* multi-hop questions
* table questions
* formula questions
* figure questions
* questions with no answer in corpus
* ambiguous questions
* conflicting-source questions
* exact terminology questions
* question-bank questions

Evaluate retrieval independently from generation.

Critical metrics include:

Recall@K
MRR
nDCG
citation correctness
evidence support
unsupported claim rate
appropriate abstention rate

Do not optimize answer-generation prompts to hide poor retrieval.

---

# 20. REQUIRED AGENT SKILLS

Create equivalent project skills for Codex and Claude.

Canonical skill subjects:

architecture-guardian
medical-grounding
document-ingestion
retrieval-quality
rag-evaluation
frontend-quality
security-review
devops-observability
handoff-maintainer

Codex project skills:
.agents/skills/<skill>/SKILL.md

Claude project skills:
.claude/skills/<skill>/SKILL.md

Keep their substantive rules synchronized.

Each SKILL.md must have valid frontmatter containing at least:

name
description

Skills should contain focused procedures rather than duplicating all of AGENTS.md.

---

# 21. AI HANDOFF

AI_HANDOFF.md is mandatory.

Before doing substantial work:
read:

* AGENTS.md or CLAUDE.md as appropriate
* AI_HANDOFF.md
* relevant ADRs

At the end of every meaningful implementation session, update AI_HANDOFF.md with:

Current stage
Last completed work
Current branch
Last known good commit
Uncommitted changes
Tests passing
Tests failing
Architecture decisions
Files changed
Known issues
Next exact task
Do-not-do warnings
Required environment state
Migration state

Do not fill handoff with conversational history.

It must be concise operational state that another coding model can immediately continue from.

---

# 22. DEVELOPMENT RULES

Do not build everything in one uncontrolled pass.

Use vertical, testable milestones.

Recommended initial milestones:

M0
Architecture, docs, repository scaffold, Docker infrastructure.

M1
Document upload + metadata + ingestion state machine.

M2
Docling parsing + normalized document model.

M3
Hierarchical chunking + provenance.

M4
MedCPT embeddings + Qdrant indexing.

M5
Hybrid BM25 + dense retrieval.

M6
Cross-encoder reranking + context expansion.

M7
Evidence gate + answer generation + citations.

M8
Claim verification + abstention.

M9
Professional Ask UI.

M10
Library/configuration UI.

M11
Evaluation framework.

M12
Security, observability and production hardening.

Complete and verify each milestone before moving on.

---

# 23. GIT RULES

Keep the repository recoverable.

Before major changes:

* inspect git status
* inspect relevant tests

At stable milestone boundaries:

* run required tests
* update docs/ADRs
* update AI_HANDOFF.md

Never:

* force push
* delete unrelated work
* reset user changes
* mass rewrite without understanding current state
* commit secrets
* claim tests passed without running them

Do not create commits unless permitted by the current user instruction/environment policy, but keep work commit-ready.

---

# 24. CODE QUALITY

Use:

* clean architecture boundaries
* typed Python
* typed TypeScript
* explicit interfaces
* dependency injection where useful
* repository/service separation
* domain-specific error classes
* structured result types

Avoid:

* giant service classes
* hidden global configuration
* provider logic scattered through business code
* silent exception swallowing
* copy-pasted retrieval logic
* unversioned prompts
* magic thresholds

Every important configuration must be validated.

---

# 25. FIRST EXECUTION

Do NOT immediately attempt to implement the entire application.

Begin by:

1. Inspecting the repository.
2. Creating the repository architecture.
3. Creating AGENTS.md.
4. Creating CLAUDE.md.
5. Creating AI_HANDOFF.md.
6. Creating the required project skills.
7. Creating architecture documentation.
8. Creating initial ADRs.
9. Creating Docker Compose architecture for PostgreSQL, Qdrant, Redis and MinIO.
10. Creating backend/frontend skeletons.
11. Creating configuration architecture.
12. Creating initial tests verifying that the development environment starts correctly.
13. Updating AI_HANDOFF.md.

Then provide a concise implementation report containing:

* files created
* architecture chosen
* commands executed
* tests executed and results
* open risks
* next milestone

Do not claim something exists unless it exists in the repository.

Do not proceed past the agreed milestone simply to generate more code.

The primary engineering objective is not maximum implementation speed.

It is:

ACCURACY
TRACEABILITY
REPRODUCIBILITY
RETRIEVAL QUALITY
SAFE ABSTENTION
MAINTAINABILITY
TESTABILITY
CROSS-AGENT CONTINUITY
