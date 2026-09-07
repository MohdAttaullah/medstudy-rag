# Kubernetes readiness

What a Kubernetes deployment of this system needs. **No manifests, Helm chart or operator ships in
this repository**, and none was built for M12 — the brief explicitly says not to build a cloud
platform. What follows is the contract a deployment must satisfy, derived from controls that
actually exist in the code.

## Workloads

| Workload | Kind | Replicas | Notes |
|---|---|---|---|
| `api` | Deployment | ≥2 | Stateless. Remember per-replica rate limits. |
| `frontend` | Deployment | ≥2 | Static assets |
| `worker` | Deployment | ≥1 | **Memory-constrained**; `MAX_CONCURRENCY=1` per pod |
| `retrieval` | Deployment | ≥1 | Model-memory constrained |
| `dispatcher` | Deployment | 1 | Outbox dispatch |
| `migrate` | Job | 1, pre-deploy | Must complete before rollout |
| PostgreSQL, Redis, Qdrant, object store | Managed services or operators | — | Never publicly routable |

## Migration job

Run Alembic as a **Job that completes before the rollout**, never from application startup.
Multiple replicas starting concurrently would race to migrate the same schema.

```yaml
# conceptual
apiVersion: batch/v1
kind: Job
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: medical-rag-api:<tag>
          command: ["alembic", "upgrade", "head"]
          envFrom: [{secretRef: {name: medrag-secrets}}]
```

Gate the application rollout on this Job succeeding. Never run `alembic downgrade` against
production automatically.

## Probes

The liveness/readiness split is a real distinction in this codebase and the manifests must respect
it, or they will undo it.

```yaml
livenessProbe:
  httpGet: {path: /health/live, port: 8000}
  periodSeconds: 10
readinessProbe:
  httpGet: {path: /health/ready, port: 8000}
  periodSeconds: 10
startupProbe:                 # model loading takes far longer than a steady-state check
  httpGet: {path: /health/live, port: 8000}
  failureThreshold: 60
  periodSeconds: 10
```

`/health/live` touches no dependency. **Do not point liveness at `/health/ready`**: a database blip
would then make Kubernetes kill and restart every replica of a healthy service, turning a
recoverable dependency outage into an application outage. `/health/ready` names the unmet
dependencies in `unready`, so a 503 is diagnosable from the probe response.

## Resources

The worker and retrieval services load neural models and are the constraint. Requests and limits
are mandatory, not advisory — this host has already demonstrated native crashes under memory
pressure.

| Workload | Requests (indicative) | Limits |
|---|---|---|
| api | 500m / 1Gi | 2 / 2Gi |
| worker | 2 / 6Gi | 4 / 8Gi |
| retrieval | 1 / 4Gi | 2 / 6Gi |
| frontend | 100m / 128Mi | 500m / 256Mi |

**These are starting points from development-host observation, not measured production capacity.**
Size them against the actual corpus. Scale the worker by adding pods, never by raising
`MAX_CONCURRENCY`.

## Configuration and secrets

Non-secret configuration in a ConfigMap; secrets through the deployment's secret store. Prefer the
file pattern the code already supports, which works with the Key Vault CSI driver, External Secrets
and plain Kubernetes Secrets alike:

```yaml
env:
  - name: OPENAI_API_KEY_FILE
    value: /var/run/secrets/medrag/openai-api-key
volumeMounts:
  - name: medrag-secrets
    mountPath: /var/run/secrets/medrag
    readOnly: true
```

This keeps the value out of the pod's environment, where `/proc/<pid>/environ` and crash dumps
would expose it. `MEDRAG_ENVIRONMENT=production` must come from the manifest, not an image default.

## Storage

* **Model cache** — a read-only volume shared by worker and retrieval pods, or baked into the
  image. Provision at the pinned revisions; a checksum mismatch fails readiness.
* **Object store** — external; no PersistentVolume needed by the application.
* **PostgreSQL, Qdrant** — managed services or their own operators with their own storage.

The application containers need no writable persistent storage and are compatible with a read-only
root filesystem apart from `/tmp`.

## Network policy

Default-deny, then allow only:

* ingress → `frontend`, `api`
* `api`, `worker`, `dispatcher`, `retrieval` → PostgreSQL, Redis, Qdrant, object store
* `api`, `worker` → provider endpoints (egress)

Qdrant, Redis, PostgreSQL and the object store must have **no ingress from the internet and none
from the browser**. The application enforces what it can — production refuses to start with a
localhost backing-service URL, and the CSP restricts the browser to `connect-src 'self'` — but
network isolation itself is the cluster's responsibility and cannot be verified from inside the
application.

## Scaling and horizontal-scaling assumptions

Stated explicitly because getting this wrong is the likeliest operational mistake:

* **Stateless, scale freely:** `api`, `frontend`.
* **Stateful:** PostgreSQL, Qdrant, Redis, object store.
* **Single-worker constrained:** `worker` — one parse at a time per pod.
* **Model-memory constrained:** `worker`, `retrieval`.
* **Not globally coordinated:** rate limiting is per replica. Global limiting belongs at the
  ingress; without it, N replicas allow N times each budget.

## Not provided

Manifests, Helm chart, operator, HPA policies, service mesh configuration, and measured resource
figures. These are deployment artifacts and are out of scope for this milestone.
