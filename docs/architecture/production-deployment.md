# Production deployment

What a production deployment of this system requires, and what it must not do. Every control
described as existing has code and a test behind it; anything a deployment must supply is marked
as such rather than implied to be built.

**No claim of HIPAA compliance, certification or clinical validation is made anywhere in this
document.** The controls below are designed to support future compliance work; they do not
constitute it.

## Production refuses to start unhardened

`Settings.production_is_hardened` is a typed validator, not a warning. With
`MEDRAG_ENVIRONMENT=production` the process will not start unless all of the following hold, and
the error names every unmet rule without printing a secret:

| Requirement | Variable |
|---|---|
| OIDC authentication | `MEDRAG_AUTH__MODE=oidc` |
| No development identities | `MEDRAG_DEV_PRINCIPALS` empty |
| Rate limiting on | `MEDRAG_LIMITS__RATE_LIMITING_ENABLED=true` |
| Explicit HTTPS origins, no wildcard | `MEDRAG_CORS_ORIGINS` |
| Database, Redis and object-store credentials present and not development defaults | `MEDRAG_DATABASE_URL`, `MEDRAG_REDIS_URL`, `MEDRAG_S3_ACCESS_KEY`, `MEDRAG_S3_SECRET_KEY` |
| Backing services not on localhost | `MEDRAG_QDRANT_URL`, `MEDRAG_S3_ENDPOINT` |
| Models pinned offline | `MEDRAG_EMBEDDING__OFFLINE=true`, `MEDRAG_QUERY_ENCODER__OFFLINE=true` |
| A configured provider has its key | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` |

Verify before deploying:

```bash
uv run python scripts/production_preflight.py --json preflight.json
```

It reads only, prints no secret values, reduces connection strings to scheme and host, and exits
non-zero on any blocking failure. `--check-provider` additionally makes one live provider call and
is never implied.

## Identity

Production uses OIDC and is vendor-neutral: Entra ID, Auth0, Okta or any compliant issuer is
configured by issuer, audience and JWKS URI. Nothing in the code knows which is in use.

```bash
MEDRAG_AUTH__MODE=oidc
MEDRAG_AUTH__ISSUER=https://login.example.com/v2.0
MEDRAG_AUTH__AUDIENCE=api://medical-rag
MEDRAG_AUTH__JWKS_URI=https://login.example.com/discovery/v2.0/keys
MEDRAG_AUTH__ALGORITHMS='["RS256"]'
MEDRAG_AUTH__TENANT_CLAIM=tid
MEDRAG_AUTH__ROLES_CLAIM=roles
MEDRAG_AUTH__ROLE_MAPPING='{"MedRag.Reader":"reader","MedRag.Curator":"curator","MedRag.Admin":"admin"}'
```

Signature verification is unconditional. Algorithms come from this configuration and never from
the token header, so `alg: none` and RS256→HS256 downgrade are both impossible; only asymmetric
algorithms are supported, so this service never holds a key that could mint tokens. A token
carrying several mapped roles resolves to the **narrowest**, so adding a group cannot silently
widen access. An unmapped role and a missing tenant claim are both refused.

The development bearer adapter is unavailable in production through two independent barriers: the
settings validator above, and `build_auth_provider`, which refuses again if handed a production
configuration selecting it.

## Roles

| Role | Capabilities |
|---|---|
| `reader` | `document:read`, `ingestion:read`, `ask:submit`, `conversation:read` |
| `curator` | reader plus upload, document management, all `ingestion:*` actions, and the `retrieval:search` / `generation:draft` / `generation:verify` inspector scopes |
| `admin` | curator plus `settings:read`, `settings:write`, `audit:read`, `operations:read` |

Roles are explicit subsets. Before M12 `curator` was defined as "every permission", which meant
adding any capability silently granted it to curators; enumerating each role makes a widening
deliberate. An unrecognised role grants nothing rather than raising.

The M12 brief's suggested `operations:admin` is not a separate permission: operating the pipeline
is the existing `ingestion:*` set, and duplicating it under a second name would create two places
for one authority to drift. `operations:read` **is** new, because read-only operational state had
no capability of its own and `settings:write` is the wrong authority for watching a system.

## Secrets

Development uses an ignored `.env`. Production should mount secrets as files and point the
process at them:

```bash
OPENAI_API_KEY_FILE=/var/run/secrets/openai-api-key
MEDRAG_DATABASE_URL_FILE=/var/run/secrets/database-url
```

Every managed store — Azure Key Vault via the CSI driver, AWS Secrets Manager via External
Secrets, Vault Agent, Kubernetes Secrets, Docker secrets — presents secrets as files, so this
supports all of them and depends on none. It also keeps the value out of the process environment,
where `/proc/<pid>/environ` and crash reporters would expose it. Only the known secret names are
resolved; a direct value wins so a local `.env` keeps working; an empty or unreadable file is a
startup failure naming the path, never the value.

Secrets are never returned by any API. The settings and operations surfaces report credentials as
presence booleans only.

## Network topology

Only the frontend and the public API should be externally reachable.

```
browser ──HTTPS──> ingress/TLS ──> frontend (static) 
                                └─> API ──┬─> PostgreSQL   (private)
                                          ├─> Qdrant       (private)
                                          ├─> Redis        (private)
                                          ├─> object store (private)
                                          └─> provider API (egress only)
worker ─────────────────────────────────────> same private backing services
```

The browser reaches only the application origin: the CSP sets `connect-src 'self'`, and no
provider endpoint or object-store URL is ever handed to it. Qdrant, Redis, PostgreSQL and the
object store must not be publicly routable. Compose binds them to `127.0.0.1` for local work.

Egress from the API and worker to the configured provider endpoint is the only outbound
requirement.

## TLS, headers and CORS

TLS terminates at the ingress; this application does not terminate TLS. Set
`MEDRAG_LIMITS__HSTS_ENABLED=true` **only where TLS termination is controlled** — sending HSTS
from a service reached over plain HTTP pins browsers to HTTPS a host does not serve, and users
cannot easily undo that.

Security headers are applied to every response including errors: CSP, `X-Content-Type-Options`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy`,
`Cross-Origin-Resource-Policy` and a `Permissions-Policy` denying camera, microphone, geolocation
and payment. The CSP deliberately permits `img-src 'self' data: blob:` and `media-src` so the
source viewer can render page previews and figure crops; a stricter policy would break the
evidence inspection that lets a reader check a citation.

CORS origins must be explicit HTTPS entries. A wildcard is refused outright on a credentialed API.

## Rate limits and request bounds

| Budget | Default per principal per minute |
|---|---|
| `ask` | 20 |
| `upload` | 10 |
| `settings_write` | 12 |
| `diagnostics` | 60 |

**These are per replica, not global.** N replicas allow N times the budget. This is stated plainly
because the alternative — a Redis-backed limiter here — would imply a global guarantee it could not
keep across a fleet. Global limiting belongs in the ingress, where all traffic is visible. Health
and readiness are never limited: throttling a probe turns load into an outage.

JSON bodies are capped at 256 KB and refused with 413 before parsing. Uploads stream and are
bounded separately by `MEDRAG_INGESTION__MAX_UPLOAD_BYTES`.

## Health and readiness

`/health/live` touches no dependency and reports only that the process is serving. A liveness probe
that failed during a database blip would make the orchestrator restart every replica of a healthy
service, turning a recoverable dependency outage into an application outage.

`/health/ready` checks PostgreSQL, Redis, Qdrant and the object store, returns 503 when any is
unavailable, and **names the unmet dependencies** in `unready` so a 503 is diagnosable. It returns
no connection string and no exception text.

Configure the orchestrator accordingly: liveness on `/health/live`, readiness on `/health/ready`,
and a startup probe generous enough for model loading in the worker and retrieval services.

## Migrations

Run Alembic as a **pre-deploy job**, never from application startup: multiple replicas starting
concurrently would race to migrate.

```bash
uv run alembic upgrade head    # migration job, one replica, before rollout
```

Application containers must not run migrations. **Never downgrade a production schema
automatically.** The M9 and M10 downgrades refuse while history exists unless the operator sets the
explicit loss-acknowledgement variable, and that variable must never be set against production
without a deliberate, recorded decision. Rollback of a schema change is a restore-from-backup
operation, not a downgrade.

## Model provisioning

All three MedCPT models are pinned by exact revision and SHA-256 and load offline. Provision the
cache into the image or a mounted volume before serving:

```bash
uv run python scripts/provision_embedding_model.py
uv run python scripts/provision_query_model.py
uv run python scripts/provision_reranker_model.py
```

Production refuses to start unless `offline` is true for the embedding and query encoders, so a
deployment that forgets the variable cannot silently fetch a different revision at query time. A
checksum mismatch fails readiness rather than serving a different vector space.

## Scaling

| Service | Scaling |
|---|---|
| API | Stateless; scale horizontally. Remember the per-replica rate-limit multiplication. |
| Frontend | Static; scale freely |
| Worker | **Constrained.** Docling and the encoders are memory-heavy; this host has demonstrated native crashes under memory pressure. `MAX_CONCURRENCY=1` per worker; scale by adding workers with memory limits, not by raising concurrency. |
| Retrieval/reranker | Model-memory constrained; scale by replica with bounded torch threads |
| PostgreSQL, Qdrant, Redis, object store | Stateful; managed services or operators |

Set container memory and CPU limits on the worker and retrieval services. These are **not**
currently set in `compose.yaml` — a documented gap, not an implemented control.

## Containers

Images run as a non-root user (uid 10001), install from the committed lockfile with `--frozen`,
and contain no secret in any layer. Base images are pinned by tag; pinning by digest is
recommended and not yet done.

## What production still requires that this repository does not provide

Listed explicitly rather than implied:

* Malware scanning of uploads.
* Global (cross-replica) rate limiting.
* Container resource limits in the shipped compose file.
* Base images pinned by digest.
* Automated backup scheduling — the procedure is documented and tested manually; no scheduler ships.
* Off-host audit log shipping.
* An automated deletion workflow (see [retention](retention.md)).
