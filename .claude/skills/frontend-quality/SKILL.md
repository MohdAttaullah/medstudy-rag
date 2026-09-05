---
name: frontend-quality
description: Build accessible medical evidence UI states and verify frontend workflows without misleading users.
---

# Frontend Quality

Read the active milestone and docs/architecture/grounding.md. Show implemented capabilities honestly; keep unavailable actions disabled and explain their availability. Build explicit loading, empty, error, insufficient-evidence and conflict states.

Preserve keyboard navigation, labels, visible focus, responsive layouts and source links. Do not show raw model confidence as medical certainty or display unverified streamed claims. Retrieve debug data only through authorized server APIs.

Use the configured TanStack Query client and typed API boundaries. Run focused unit tests, TypeScript/build checks and meaningful browser checks for modified flows. Inspect desktop and narrow layouts when layout changes; document anything not exercised.
