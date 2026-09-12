# AI HANDOFF

## Current state

**M12 COMPLETE and verified. This is the final planned milestone; M13 was not created.**

Baseline main `10930c4` (committed M11), clean at start. All M12 work is uncommitted; nothing
staged; no history rewritten. M0–M11 were not restarted or redesigned.

**The safety pipeline is unchanged.** M12 added barriers around it and removed none: no fast path,
no verification-disabled mode, no provider-direct answering, no fallback to pretrained knowledge. A
test asserts the codebase contains no such switch. M9 (64), M10 (48) and M11 (92) all pass unchanged.

See `docs/verification/m12.md` and `docs/adr/017-m12-production-hardening.md`.

## Deployment requirements

Production is a configuration the system **refuses to run badly**. With
`MEDRAG_ENVIRONMENT=production`, `Settings` will not construct unless all of the following hold,
and the error names each unmet rule without printing a value:

OIDC auth mode · no development principals · rate limiting enabled · explicit HTTPS CORS origins ·
database/Redis/object-store credentials present and not development defaults · backing services not
on localhost · `MEDRAG_EMBEDDING__OFFLINE` and `MEDRAG_QUERY_ENCODER__OFFLINE` true · a configured
provider has its key.

```bash
uv run python scripts/production_preflight.py            # read-only, no provider call
uv run python scripts/production_preflight.py --check-provider   # opt-in live call
```

Full detail: `docs/architecture/production-deployment.md`.

## Production authentication

Vendor-neutral OIDC — issuer, audience, JWKS URI and claim names are configuration; no code knows
the provider. Signature verification has no disable flag; algorithms come from configuration not the
token header; asymmetric only; a missing tenant claim is refused rather than defaulted; unmapped
roles grant nothing; several mapped roles resolve to the narrowest.

Development auth has **two** independent barriers: the settings validator and `build_auth_provider`.

```bash
MEDRAG_AUTH__MODE=oidc
MEDRAG_AUTH__ISSUER=https://login.example.com/v2.0
MEDRAG_AUTH__AUDIENCE=api://medical-rag
MEDRAG_AUTH__JWKS_URI=https://login.example.com/discovery/v2.0/keys
MEDRAG_AUTH__ROLE_MAPPING='{"MedRag.Reader":"reader","MedRag.Admin":"admin"}'
```

## Secrets

Development: ignored `.env`. Production: mount files and use `<NAME>_FILE`, which every managed
store provides and which keeps values out of the process environment. A direct value wins; only
known names resolve; an empty or unreadable file fails startup naming the path, never the value.

## Services and migration

Nine long-running services: postgres, redis, qdrant, minio, api, frontend, worker, retrieval,
dispatcher. Only frontend and API are externally reachable. **Health, readiness and metrics are not
proxied through the public origin** — they are internal surfaces.

Run Alembic as a **pre-deploy job**, never from application startup; replicas would race. Never
downgrade a production schema automatically. Alembic head is `m10_configuration`; **M12 added no
migration**, which is correct for a hardening milestone.

## Model provisioning

Three MedCPT models pinned by revision and SHA-256, loaded offline. Provision before serving with
`scripts/provision_{embedding,query,reranker}_model.py`. Production refuses to start unless the
encoders are offline-pinned, so a forgotten variable cannot cause a silent revision change.

## Backups

`pg_dump` for PostgreSQL, `aws s3 sync` for originals. Qdrant is **rebuilt, not restored**.
Verify every restore into a disposable target:

```bash
uv run python scripts/verify_restore.py --database "$RESTORE_URL"
```

It refuses to target the configured live database. A real restore was exercised: 7/7 checks passed.
No scheduler ships. No RPO/RTO is claimed. See `docs/architecture/backup-and-recovery.md`.

## Commands

```bash
# tests — fresh process per group (Docling crashes natively under memory pressure)
python .local/m12-regression-run.py                    # 878 across 11 groups
uv run pytest backend/tests/test_m12_units.py -q

# static
uv run ruff check backend workers scripts && uv run mypy backend/app

# evaluation (offline, no API key, blocking in CI)
uv run python scripts/evaluate_m11.py

# operations
uv run python scripts/smoke_m12.py                     # 33 checks against the live stack
uv run python scripts/load_test.py --reads 200         # deterministic by default
uv run python scripts/load_test.py --ask 12 --live-provider    # opt-in, costs money
```

## Post-M12 remediation (no new milestone)

Three pieces of work sit after M12 and are **not** a milestone. M13 was not created.

Committed: `0a52258` clean development workspace utility, `74ccd42` large-PDF upload limits
(application 512 MiB, proxy 520 MiB, size-scaled validation budget).

**Uncommitted: large-document parsing memory.** A real 153 MiB / 932-page medical textbook now
uploads and validates, but parsing killed the Celery child with SIGKILL at 6.2 GB
(`memory.max_usage_in_bytes` 6209466368, `oom_kill 1`, the container itself surviving) and the
document sat in `PARSING`. Three causes, all measured rather than inferred:

| Cause | Measurement | Change |
|---|---|---|
| Docling retains per-page state for a whole `convert()` call | 44 MiB/page on real textbook content; a 50-page call peaked 2442 MiB | convert in 25-page windows joined by absolute page number |
| The source text layer was read for the whole book up front | ~1.3 GiB before Docling converted a page | read it one window at a time |
| glibc kept freed arenas instead of reusing them | RSS ratcheted ~6 MiB/page while retained output grew ~0.2 MiB/page | trim between windows; `MALLOC_ARENA_MAX=2` |

Result on the same book: **932/932 pages, 18,888 elements, 263 tables, 1,208 figures, peak worker
4967182336 bytes (4.74 GiB) against the previous 6209466368, parse 4042 s.** Peak is now bounded by
`page_window_size`, not by book length — settled RSS over eight windows went from climbing
2267 → 2842 MiB to flat 1248 → 1321 MiB.

The book parsed to **NEEDS_REVIEW**: 2 pages of 932 raised `PAGE_CONTENT_LOST`. That is the
fail-closed gate working, and neither finding is a windowing artefact. Page 44 is a section-divider
page that yields the same three elements at every conversion size. Page 517 carries only the tail of
a paragraph that begins on page 516, which Docling anchors to the paragraph's first page — the text
is present in the parse, on page 516. Both are provenance-precision limits, not content loss; see
`docs/architecture/document-parsing.md`.

**Uncommitted: reviewed parse acceptance** (ADR-018), which closed the gap that left this document
stuck. A flagged parse keeps `validation_result = NEEDS_REVIEW` and every finding forever; an
authorized curator's acceptance is an append-only `parse_review_decisions` row bound to one exact
parse run, and the run becomes `ParseRunStatus.REVIEWED_ACCEPTED`. The job moves
`NEEDS_REVIEW -> READY_FOR_CHUNKING` **without consuming a retry**, because nothing is reprocessed.
Permission `ingestion:accept`, curator and admin only. Migration `parse_review_decisions`
(down_revision `m10_configuration`) moves six enforcement points together — the status vocabulary
and column width, `ck_parse_runs_active_run_usable`, `m1_job_guard`, **both** clauses of
`m3_run_guard`, and the new `parse_review_guard`. The `m3_run_guard` activation clause is the
subtle one: it keys on `validation_result`, so leaving it behind would let chunking finish and then
refuse to activate its own dataset. Anything gating on "is this parse usable" must consult run
**status**, never `validation_result` alone.

The real book was accepted through this flow on 2026-09-11: run `3ebd8b90` is `REVIEWED_ACCEPTED`
and active, `validation_result` still `NEEDS_REVIEW`, all 10 findings unchanged, `retry_count`
still 2/3, chunking ran and produced 4,253 chunks.

**New downstream blocker, not fixed and not caused by this work:** embedding failed with
`EMBEDDING_INPUT_TOO_LONG`. **619 of 4,253 chunks exceed MedCPT's 512-token input limit** (618
`TEXT_PARENT`, 1 `TABLE_PART`; worst 1,280 tokens). `max_input_tokens` is `Literal[512]` — pinned
to the model, not tunable — and the embedder refuses to truncate by design, so nothing was
silently dropped. This is a chunking-policy/embedding-limit mismatch that a 932-page textbook is
the first document to expose; the job is `FAILED` with `retry_count` still 2/3. Nothing downstream of parsing has
been exercised at this scale; chunking and embedding an 18,888-element document is untested.

The lease now heartbeats between windows (`PARSE_WINDOW_COMPLETED`), which is what lets a
multi-hour parse keep a lease sized for liveness. Recovery was observed for real: a worker lost at
13:10 was reaped at 13:40 to `FAILED` / `PARSER_LEASE_EXPIRED` and the job retried successfully.

Time budgets moved with it: one conversion call 900 s, whole document 900 s + 12 s/page capped at
14400 s, Celery soft/hard 15600 s / 15900 s. A validator enforces the ordering.

**No claim of 300–500 MB support is made.** Only this 153 MiB / 932-page book has been run.

## Known risks

Security: no malware scanning; rate limiting is **per replica, not global**; base images pinned by
tag not digest; no token-revocation check; audit tamper-evidence stops at the database boundary;
`MEDRAG_ENVIRONMENT` can be wrongly set to `development`; prompt-layer injection defence is
probabilistic (the structural guarantee is M8's deterministic checks).

Operational: no backup scheduling, PITR or cross-region replication; no RPO/RTO; no complete
deletion workflow (its central policy conflict is undecided); no container resource limits in
compose (the worker's memory ceiling is the host's, so a parse that outgrows it is still killed by the kernel rather than refused); no CI config or Kubernetes manifests ship; no OTel exporter or alerting deployed; worker
is one parse per pod — scale by adding workers, never by raising concurrency. Four
`GENERATION_SCHEMA_VIOLATION` outcomes were observed under concurrent vague questions with no
pre-M12 baseline for comparison.

Quality — **unchanged from M11 and not hidden by hardening**: EvidenceSet coverage 0.933 not 1.0;
verifier is the same model as the generator so verification is not independent; the conflict
detector misses disagreements phrased with different label windows; visual interpretation
unavailable; evaluation is synthetic-only and nothing is expert-reviewed.

## Boundary

**No claim of HIPAA compliance, HIPAA certification, clinical validation, production certification
or absence of hallucination is made anywhere.** The controls support future compliance work; they do
not constitute it.

**M12 is the final planned milestone. Do not create M13.** Remaining gaps are classified in
`docs/verification/m12.md` §32 as production blockers, known operational limitations, future
enhancements, or clinical-validation requirements. No automatic commit.
