# AI HANDOFF

## Current milestone

M10 COMPLETE and verified. Baseline main `7f3b857` (committed M9). All M10 work is uncommitted;
nothing staged; no history rewritten. M0–M9 were not restarted. M11/M12 have NOT started.

Tree: 23 modified, 19 new. See `docs/verification/m10.md` for the full report and
`docs/adr/015-m10-controlled-configuration.md` for the decisions.

## Implemented

Explicit typed registry in `backend/app/configuration/registry.py`: 125 settings, 10 sections,
43 editable (33 RUNTIME_SAFE + 10 rebuild proposals), 82 read-only, 61 of them IMMUTABLE. Every
entry carries a backend-owned lifecycle; the frontend renders that metadata and derives none of it.

Tenant-scoped append-only revisions with preview digests, optimistic concurrency, tenant row
locking, mixed-activation refusal and audit integration, at `/api/v1/settings`. `settings:read` and
`settings:write` are admin-only. Runtime-safe changes apply to subsequent requests through a copied
service graph; chunk/analyzer changes stay PENDING_REBUILD and never move an effective value. Ask
stores the exact allowlisted snapshot with each turn. Shared service config and safety invariants
are read-only; no secret is editable or returned. Approved model pairs come only from startup
config. React administration UI at `/settings`.

Migration `m10_configuration` revises `m9_conversations`; applied, no drift, append-only history
trigger, downgrade guarded. Never set `MEDRAG_ALLOW_CONFIGURATION_LOSS` against the live DB without
explicit authorization.

## Fixed during this session

1. **M10 was stricter than M6.** The registry refused `evidence_budget.max_tokens_per_block >
   max_total_tokens`. M6 declares no such relation and applies both caps independently;
   `test_m6_units.py:217` builds that exact state. Rule removed. The declined patch's other half —
   removing the `reranking.candidate_top_k` check — was **not** applied: that rule is M5's own
   (`RetrievalConfig` rejects `final_top_k > dense+sparse`, and M6 assigns `candidate_top_k` there
   via `model_copy`, which skips validators). Both directions are now pinned by regression tests.
2. **`scripts/smoke_m8.py` asserted a non-invariant.** It required `verified is False` on the M8
   diagnostic endpoint, contradicting its own later assertions and comment. `answering_enabled is
   False` — the real pinned invariant — is still asserted; nothing was weakened.
3. Migration filename/docstring aligned to the revision id; four architecture doc addenda rewrapped;
   README paragraph rewrapped; validation description corrected in configuration-management.md.

## Verification

Backend regression, fresh process per group (native Docling crashes under memory pressure here):
179 + 28 + 71 + 148 + 44 + 73 + 52 + 64 + 48 = **707 passed**, matching `--collect-only` exactly.
ruff clean, format 248 files, mypy 179 files, skills 9 pairs, alembic `m10_configuration (head)`
with no drift and a full round trip on a disposable schema. Frontend **75 passed** + build.
Playwright **17 passed** on a drained queue (desktop + 390 px mobile). Docker 9 services healthy on
freshly built M10 images. Smokes **PASS ×17**, including a live configuration→Ask run reaching
VERIFIED at revision 7.

## Next work

M11 is the evaluation/quality milestone and has not been started. Do not begin it without an
explicit request.

Operational notes: `worker` and `retrieval` were stopped during regression to free memory and have
been restored — restore them before any browser or live verification. Three orphaned `QUEUED`
ingestion rows predate this work; both Celery queues are empty. Playwright's browser had to be
installed manually from Google's official chrome-for-testing artifact because Playwright's download
host returns `400` from this network; the build is the exact revision Playwright pins.

Credentials remain in ignored `.env`; never print them. Use `uv run --cache-dir .uv-cache --extra
parsing --extra embedding --env-file .env` with `MEDRAG_RUN_INTEGRATION=1`. No automatic commit.
