# FILE: CLAUDE.md

# Enterprise Medical RAG — Claude Code Instructions

This repository implements an enterprise, accuracy-first medical Retrieval-Augmented Generation platform.

The system is designed around the principle:

> It is preferable to abstain than to provide an unsupported medical answer.

## At the beginning of every work session

Read:

1. `AI_HANDOFF.md`
2. relevant `docs/architecture/*`
3. relevant `docs/adr/*`
4. relevant `.claude/skills/*/SKILL.md`

Inspect repository and Git state before assuming the handoff is still accurate.

Repository state overrides stale conversation context.

## Cross-agent development

This repository may alternate between Codex and Claude Code.

Do not assume that work performed by another agent is wrong or incomplete merely because it was not created in this session.

Continue from actual repository state.

Do not repeat already completed milestones.

Before ending substantial work, update `AI_HANDOFF.md` so Codex or another Claude session can continue without reconstructing the project from conversation history.

## Durable architecture

Core stack:

* React + TypeScript
* FastAPI + Python
* PostgreSQL
* Qdrant
* Redis + Celery
* MinIO/S3
* Docling

Core RAG path:

Docling parsing
→ structure-aware hierarchical chunking
→ dense MedCPT retrieval

* sparse/BM25 retrieval
  → fusion
  → MedCPT Cross-Encoder reranking
  → context expansion
  → evidence sufficiency
  → provider-independent grounded LLM
  → claim verification
  → citation validation
  → answer or abstention

Optional retrieval components such as ColBERT must remain benchmark-driven.

## Grounding requirements

Never intentionally introduce a path where the model can answer unsupported medical claims from pretrained memory.

Every substantive generated claim must have evidence.

If evidence is insufficient, abstain.

If authoritative evidence materially conflicts, report conflict/abstain according to configured policy.

Question-bank answers are not automatically authoritative truth.

Generated descriptions of tables/figures are metadata and may not replace original evidence.

The original document/page/figure remains source material.

## Provider independence

Keep OpenAI-, Anthropic- and other provider-specific behavior behind adapters.

Domain and retrieval layers must not import vendor SDKs directly unless they themselves are provider adapters.

Model IDs are configuration.

Do not redesign architecture around a temporary model release.

## Development behavior

Prefer small, validated vertical increments.

Read existing implementations before rewriting.

Do not create duplicate services simply because you did not notice an existing abstraction.

Do not silently change:

* public APIs
* persistent schemas
* vector schema
* grounding policy
* index semantics

when a migration/ADR is required.

## Testing

Run actual commands before claiming success.

For every safety or retrieval regression, create a regression test.

Do not disable tests to make the project appear healthy.

Evaluation quality is part of implementation quality.

## Documentation

Use ADRs for meaningful architectural decisions.

Update architecture documentation when implementation materially changes.

Never describe unimplemented behavior as implemented.

## Security

Uploaded documents are untrusted.

Never commit secrets.

Never leak provider tokens.

Avoid logging full source documents or medical questions unless development logging is explicitly configured for a safe corpus.

Server-side authorization is mandatory.

## Git safety

Inspect `git status` before modifications.

Preserve work from other agents and the user.

Never force-push, hard-reset, rewrite history or discard unrelated changes without explicit authorization.

## Skills

Use project skills under `.claude/skills/` when relevant, especially:

* architecture-guardian
* medical-grounding
* document-ingestion
* retrieval-quality
* rag-evaluation
* frontend-quality
* security-review
* devops-observability
* handoff-maintainer

Keep these substantively aligned with corresponding Codex skills.

## Completion

Before declaring work complete:

* implementation exists
* tests were executed
* results are reported accurately
* migrations are accounted for
* docs are updated where necessary
* architectural invariants remain intact
* AI_HANDOFF.md is current

---

