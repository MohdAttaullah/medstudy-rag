# ADR-006: Durable upload control plane and receipt-only queue

Status: Accepted. Date: 2026-09-05. Scope: M1.

## Context

PostgreSQL, private S3 storage and Celery do not share a transaction. Upload retries, storage/database
failure and duplicate delivery must preserve original provenance without creating uncontrolled
orphans or implying that parsing has occurred. Production identity is deferred, but data endpoints
already require server-side authorization.

## Decision

Use a durable UploadIntent saga. Reserve idempotency and UUID object identity in PostgreSQL before
S3 writes, fence finalization with an intent lock, and commit document/version/job/history/outbox
together. Resolve ambiguous commits before deleting anything. Reconcile only expired or failed
uncommitted keys, retaining failed cleanup for retry. Store and pin the actual S3 version ID.

Use a transactional outbox with stable UUID task IDs and job retry generation. Publish until a
durable receipt exists. The Celery receiver checks original metadata and commits receipt while
leaving the job QUEUED. Locking and generation checks fence cancelled/stale deliveries.

Accept raw PDF bodies with base64 JSON metadata in a bounded header. This authenticates and enforces
stream limits before multipart buffering. It also enables actual XHR transfer progress. One endpoint
creates a publication plus its first version; a second adds a version. No redundant upload route.
Use a server-configured development bearer adapter behind AuthProvider, with tenant-scoped reader,
curator and admin permissions. It is explicitly not production authentication.

Keep stage history ordered by a per-job sequence; timestamps are not a reliable total order.
Freeze the ingestion configuration snapshot on each job. M1 cannot activate/search any version.

## Consequences

Uploads can be retried safely after ambiguous client failures. Exact bytes deduplicate inside tenant
scope; committed originals survive recovery. Small metadata must fit the 12,000-character encoded
header limit, and reverse proxies must retain /api/v1, permit that header size and disable upload
buffering. Large uploads spool to disk and hold an intent transaction during S3 finalization;
future throughput work may replace that lock with a lease only with equivalent fencing tests.

Redis delivery is at least once; PostgreSQL remains authoritative. API, dispatcher and receiver must
be deployed together for receipt confirmation. M2 needs new stage handlers, leases, processing
artifacts and migrations before any executable state beyond QUEUED. Antivirus, parser resource
isolation, OIDC and production operations remain separate hardening work.
