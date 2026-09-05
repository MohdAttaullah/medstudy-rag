---
name: handoff-maintainer
description: Update AI_HANDOFF.md at milestone boundaries or before another coding agent continues work.
---

# Handoff Maintainer

Read actual Git state, current implementation, executed test results and relevant ADRs. Replace stale operational state rather than appending a conversation log.

Record milestone/status, completed files, branch, last known good commit (or no baseline), staged/uncommitted changes, tests and exact commands, migration state, infrastructure state, decisions, issues and blockers. Distinguish failed checks, skipped checks and unattempted work.

Provide the next exact task with required environment and do-not-do constraints. Do not advance to another milestone merely because some checks are blocked. Keep the handoff concise and match completion claims to repository evidence.
