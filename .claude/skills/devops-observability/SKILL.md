---
name: devops-observability
description: Change local infrastructure, service startup, telemetry or production deployment architecture.
---

# Devops Observability

Read docs/architecture/deployment.md and observability.md. Validate Compose without printing resolved secrets. Separate process liveness, dependency readiness and future corpus/index readiness. Do not assume a container is usable just because it started.

Keep state in named services and workers independently scalable. Document environment/image versions, migration ordering and recovery. Avoid destructive volume cleanup; do not rotate existing database credentials by merely editing environment variables.

Run real protocol checks when the engine is available and report skipped infrastructure checks when it is not. Use allowlisted logs, bounded-cardinality metrics and correlation IDs. Never report a scrape example or deployment contract as a running monitoring/Kubernetes installation.
