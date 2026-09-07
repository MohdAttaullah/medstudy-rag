# CI/CD and deployment promotion

The pipeline this repository is built to support. **No CI configuration ships here** — the stages
below are documented and every command in them is one that exists and has been run locally. Stating
that plainly matters: a document describing pipelines that were never executed would be worse than
none.

## Stages

| Stage | Command | Blocking |
|---|---|---|
| Lint | `ruff check backend workers scripts` | yes |
| Format | `ruff format --check backend workers scripts` | yes |
| Types | `mypy backend/app` | yes |
| Backend tests | fresh-process partition (below) | yes |
| Frontend tests | `npm run test -- --run` | yes |
| Frontend build | `npm run build` | yes |
| Skill sync | `python scripts/check_skills.py` | yes |
| Migration validation | `alembic upgrade head && alembic check` on a disposable database | yes |
| **Offline safety evaluation** | `python scripts/evaluate_m11.py` | **yes** |
| Dependency audit | `pip-audit`, `npm audit` | yes for high/critical |
| SBOM | `cyclonedx-py environment .venv` | artifact |
| Container build | `docker compose --profile app --profile workers build` | yes |
| Browser tests | `MEDRAG_E2E_LIVE=1 npx playwright test` | yes, on a drained queue |
| Preflight | `python scripts/production_preflight.py` | yes, per environment |
| Live provider evaluation | `python scripts/evaluate_m11.py --live` | **no — manual only** |

## The safety gate

`scripts/evaluate_m11.py` is the gate that matters, and it is designed to be runnable in CI: it
needs no API key, makes no provider call, and touches neither PostgreSQL nor Qdrant. It exits
non-zero when a quality gate fails **or when a hard safety invariant went unmeasured**, because a
gate that did not run has not been upheld.

The invariants it protects:

```
false PASS on the frozen safety suite      = 0
false allow (generation on insufficient)   = 0
invented citation ids                      = 0
answer without a VERIFIED outcome          = 0
answered when the gold says unanswerable   = 0
cross-tenant leakage (contract level)      = 0
unclassified failure reason codes          = 0
```

**The held-out suite is not a tuning loop.** If a change makes it fail, the change is wrong; the
dataset is not adjusted to accommodate it. Adjusting gold to make a build green would destroy the
one property that makes the set held out.

## Paid provider calls never run on an untrusted PR

`--live` is never implied by another flag, and the live smokes are separate scripts. A pull request
from outside the organisation must not be able to spend the provider budget or exfiltrate a key
through a modified workflow, so provider-backed jobs run only on protected branches with the
secret bound to a protected environment.

## Backend test partition

The suite is run as fresh processes, one per group, because Docling and the neural models crash
natively under memory pressure when the whole suite shares one interpreter. This is a real
constraint of the runner, not a stylistic choice.

```
base | m2 (Docling, isolated) | m4 | m5 | m6 | m7 | m8 | m9 | m10 | m11 | m12
```

The partition must cover `pytest backend/tests --collect-only` exactly once — verified by summing
the group counts and comparing to the collected total.

## Environments

| Environment | Identity | Data | Providers |
|---|---|---|---|
| development | Development bearer identities | Local synthetic corpus | Optional |
| test | Development identities, disposable schemas | Fixtures only | Fake providers |
| staging | OIDC against a non-production tenant | Synthetic or approved sample | Real, low budget |
| production | OIDC | Real corpus | Real |

Configuration and secrets are environment-specific and supplied by the deployment, never baked into
an image. `MEDRAG_ENVIRONMENT` must be set by the pipeline rather than defaulted in the image —
otherwise a production rollout that forgot it would run with development defaults, and no system
can distinguish that from an actual development environment.

Production refuses to start unhardened, so a misconfigured promotion fails at startup rather than
serving insecurely. That is the intended failure mode: a rollout that will not start is visible,
and one that starts with development authentication is not.

## Model, index and corpus promotion

These do **not** flow with the application image, and must not.

* **Models** are pinned by revision and SHA-256 and provisioned into the image or a mounted cache.
  Production refuses to start unless the encoders are offline, so a promotion cannot silently
  introduce a different revision.
* **Indexes** are built and verified in the target environment. Vector semantics are never mutated
  in place; a new index run is activated only after verification, and the previous one stays active
  until then.
* **Corpora** are tenant data and never promoted between environments. Copying a production corpus
  into staging would move confidential documents into a lower-trust environment.

Each is auditable: index runs record model identity and revision, and configuration changes are
immutable revisions with an actor and a correlation id.

## Rollback

Application rollback is redeploying the previous image. It is safe when the schema is unchanged.

**Schema rollback is not an Alembic downgrade.** Production schemas are never downgraded
automatically; the M9 and M10 downgrades refuse while history exists. Recovering from a bad
migration is a restore-from-backup operation. See [backup and recovery](backup-and-recovery.md)
and the [runbooks](../runbooks.md).
