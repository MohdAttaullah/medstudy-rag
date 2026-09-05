# Production deployment contract (not deployable manifests)

M0 is development-only. Before adding Kubernetes manifests, implement and test OIDC/RBAC, tenant
isolation, external secrets, TLS, private ingress, network policies, non-root/read-only workloads,
resource limits and encrypted persistence. Use `/health/live` for liveness and `/health/ready` for
dependency readiness; corpus readiness requires additional checks once implemented.

Deploy API replicas independently from ingestion, embedding and validation queue consumers. Use
dedicated GPU scheduling where model benchmarks require it. Migrations run as an explicit release job.
Design backup/restore and object/index reconciliation before defining rollout and rollback procedures.
Never reuse local MinIO root credentials or publish internal metrics without access controls.
