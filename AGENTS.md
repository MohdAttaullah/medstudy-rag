# AGENTS.md

## Project

This repository contains an enterprise-grade, accuracy-first medical Retrieval-Augmented Generation platform.

The product answers educational medical questions using an explicitly indexed corpus of textbooks, reference material, tables, formulas, figures, question banks and question papers.

The core safety principle is:

> Unsupported answers are unacceptable. Abstention is an intended successful outcome.

## Required startup reading

Before substantial work, read:

1. `AI_HANDOFF.md`
2. relevant files under `docs/architecture/`
3. relevant ADRs under `docs/adr/`
4. relevant project skills under `.agents/skills/`

Do not rely on conversational history when repository state provides newer information.

## Source of truth hierarchy

Current repository code and tests are the implementation source of truth.

Accepted ADRs describe intentional architecture.

`AI_HANDOFF.md` describes current implementation state.

This file defines durable engineering constraints.

If these disagree, investigate before making assumptions.

## Architecture

Primary stack:

* FastAPI
* Python
* Pydantic v2
* SQLAlchemy 2
* PostgreSQL
* Qdrant
* Redis
* Celery
* MinIO/S3-compatible storage
* Docling
* React
* TypeScript
* Vite
* Tailwind
* TanStack Query

Retrieval architecture:

* structure-aware Docling parsing
* hierarchical parent/child chunks
* MedCPT dense retrieval
* BM25/sparse retrieval
* RRF or benchmarked fusion
* MedCPT Cross-Encoder reranking
* optional late-interaction retrieval
* context expansion
* evidence sufficiency gate
* grounded generation
* claim-level verification
* citation validation
* abstention

## Medical grounding invariants

Never weaken these invariants without an explicit ADR.

1. The LLM is not authoritative medical evidence.
2. Answers must be supported by retrieved corpus evidence.
3. A material unsupported claim invalidates the answer.
4. Insufficient evidence results in abstention.
5. Materially conflicting authoritative evidence must not be silently resolved by model preference.
6. Question-bank keys are not automatically authoritative.
7. Original source artifacts remain the source of truth.
8. Generated captions, summaries and rewrites are metadata, not authoritative evidence.
9. Every citation must resolve to real stored provenance.
10. Partially ingested documents must not become searchable.

## Provider independence

Do not embed provider-specific calls throughout the application.

Use explicit provider abstractions for:

* generation
* structured generation
* vision
* embeddings where applicable
* verification

OpenAI/Anthropic implementation details belong inside adapters.

Do not hard-code a frontier model ID in domain logic.

## Configuration

All operational thresholds belong in typed configuration.

Classify settings as:

* runtime-safe
* reindex-required
* restart-required

Changing an embedding model, incompatible chunking strategy or vector schema must create a new version/reindex workflow.

Do not mix incompatible embeddings in the same unnamed vector field.

## Ingestion

Ingestion is idempotent, observable and versioned.

Expected path:

`UPLOADED → VALIDATING → QUEUED → PARSING → NORMALIZING → CHUNKING → ENRICHING → EMBEDDING → INDEXING → VERIFYING_INDEX → READY`

Failure states include:

`FAILED`, `QUARANTINED`, `NEEDS_REVIEW`, `CANCELLED`.

Only fully validated document versions become active/searchable.

## Retrieval quality

Do not "fix" poor retrieval by increasing LLM context blindly.

Measure retrieval independently.

Important metrics include:

* Recall@K
* MRR
* nDCG
* reranker improvement
* citation correctness
* evidence coverage
* unsupported claim rate
* abstention behavior

Changes to chunking, embeddings, retrieval, fusion or reranking require eval comparison.

## Database and provenance

Use UUID identifiers.

Maintain document and index versioning.

Every relevant chunk should retain enough metadata to resolve:

chunk
→ element
→ page
→ document version
→ document

Preserve page/bounding-box provenance when available.

## Testing

Never say a test passes without running it.

For changed functionality:

* run focused tests first
* run broader affected suites afterward

Add regression tests for bugs.

Critical safety/retrieval behavior requires tests.

Do not delete a failing safety test simply to obtain a green suite.

## Frontend

The UI must communicate evidence state clearly.

Support:

* answer
* citations
* source preview
* insufficient evidence
* conflicting evidence
* ingestion status
* validation errors

Never represent arbitrary model self-confidence as medically meaningful certainty.

## Security

Never commit secrets.

Do not log credentials, access tokens or provider keys.

Avoid logging complete medical prompts/documents unless explicitly enabled for a safe development environment.

Enforce authorization server-side.

Treat uploaded documents as untrusted input.

## Git

Before modifications:

* inspect `git status`
* understand existing user changes

Never:

* force push
* discard unrelated work
* reset user changes
* rewrite history without explicit instruction
* commit credentials

Keep work recoverable.

## Documentation

Material architecture decisions require an ADR or update to an existing ADR.

Documentation must describe implemented behavior, not desired behavior presented as completed work.

## AI_HANDOFF.md

`AI_HANDOFF.md` is mandatory cross-agent operational state.

Before finishing a meaningful development session, update it.

Include:

* current milestone
* completed work
* branch
* last known good commit
* uncommitted state
* tests
* migrations
* decisions
* known issues
* next exact task
* blockers
* do-not-do warnings

Keep it concise.

Do not turn it into a chronological chat log.

## Skills

Use relevant project skills from `.agents/skills/`.

Expected skill domains include:

* architecture-guardian
* medical-grounding
* document-ingestion
* retrieval-quality
* rag-evaluation
* frontend-quality
* security-review
* devops-observability
* handoff-maintainer

Skills provide focused procedures.

This `AGENTS.md` remains the durable project-wide contract.

## Definition of done

A task is not complete merely because code was written.

Where applicable, done means:

* implementation complete
* type/lint checks pass
* tests pass
* migrations considered
* configuration documented
* security implications considered
* observability included
* relevant docs/ADR updated
* `AI_HANDOFF.md` updated
* no unsupported completion claims
