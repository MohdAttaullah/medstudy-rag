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

## Known risks

Security: no malware scanning; rate limiting is **per replica, not global**; base images pinned by
tag not digest; no token-revocation check; audit tamper-evidence stops at the database boundary;
`MEDRAG_ENVIRONMENT` can be wrongly set to `development`; prompt-layer injection defence is
probabilistic (the structural guarantee is M8's deterministic checks).

Operational: no backup scheduling, PITR or cross-region replication; no RPO/RTO; no complete
deletion workflow (its central policy conflict is undecided); no container resource limits in
compose; no CI config or Kubernetes manifests ship; no OTel exporter or alerting deployed; worker
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
