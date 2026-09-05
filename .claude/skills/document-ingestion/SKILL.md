---
name: document-ingestion
description: Build and validate durable document ingestion, structured parsing, provenance and staged activation.
---

# Document Ingestion

Read docs/architecture/ingestion.md and data-model.md. Define transition and retry semantics before stage handlers. Persist each attempt, configuration/model versions, correlation ID and sanitized errors. Use fenced leases and stable idempotency keys.

Preserve original files, Docling JSON, reading order, page/box coordinates and heading hierarchy. Keep tables with headers, formulas with variables, and questions with options/key/explanation. Separate generated enrichment from source elements.

Test duplicate delivery, crash recovery, cancellation, mixed configuration and concurrent activation. READY requires parse/chunk validation, complete embeddings, exact index/metadata reconciliation and retrieval smoke success. Staging data must stay unsearchable even if a worker crashes.
