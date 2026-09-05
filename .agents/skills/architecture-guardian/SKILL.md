---
name: architecture-guardian
description: Review architectural boundaries and migration impact when changing this medical RAG repository.
---

# Architecture Guardian

Read docs/architecture/system-overview.md and the ADRs affecting the change. Identify the active milestone in AI_HANDOFF.md before extending scope.

Map the change across API, services, repositories, provider adapters and workers. Check whether it changes persistent schema, vector compatibility, activation or grounding. Record an ADR for material changes and a migration/rollback plan for persistent state.

Keep PostgreSQL activation manifests authoritative; do not assume Qdrant and relational writes share a transaction. Review actual tests and distinguish an interface or empty package from an implemented capability. Update the handoff with exact remaining work.
