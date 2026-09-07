# Operational runbooks

Practical procedures for the failures this system actually has. Each begins with how to confirm the
diagnosis, because acting on the wrong one is how a short outage becomes a long one.

Shared first step for every incident:

```bash
curl -s localhost:5173/health/ready | jq          # which dependency is unready
curl -s -H "Authorization: Bearer $ADMIN" localhost:5173/api/v1/operations/status | jq
```

`/health/ready` names the unmet dependencies in `unready`. The operations endpoint gives job
counts, retry totals, the effective configuration revision, model identity and policy
fingerprints — enough to attribute an answer to the exact model and policy that produced it.

---

## Provider outage

**Symptoms.** Ask returns `outcome: FAILED` with a `GENERATION_PROVIDER_*` reason code;
`provider_calls_total` flat.

**Confirm it is the provider, not the corpus.** A provider failure is `FAILED`;
`INSUFFICIENT_EVIDENCE` means the corpus lacked evidence and is not an outage. The distinction is
deliberate and is what tells you whether to page anyone.

**Act.** No action is required for safety — the system already fails closed and will not answer
ungrounded. Confirm the provider's status page, then check the credential is still present:

```bash
uv run python scripts/production_preflight.py     # provider.generator / provider.verifier
```

If the key was rotated, update the mounted secret and restart the API. If the outage is prolonged,
the system continues to serve retrieval and abstains on Ask; that is correct degraded behaviour, not
something to work around.

**Never** disable verification or point the generator at an unapproved model to restore answers.

## Database outage

**Symptoms.** Readiness 503 with `unready: ["postgres"]`; API 5xx on data routes.

**Act.** Liveness stays green by design, so the orchestrator will not restart healthy replicas —
do not override that. Restore the database, then confirm:

```bash
uv run alembic current      # expected head
```

In-flight ingestion is recovered from durable job rows, not from the queue, so no work is lost by
the outage itself. Jobs interrupted mid-stage resume or fail with a recorded error code.

## Queue backlog

**Symptoms.** `QUEUED` job count growing in the operations endpoint; ingestion latency rising.

**Confirm.** `docker compose exec redis redis-cli LLEN ingestion`.

**Act.** The worker runs at `MAX_CONCURRENCY=1` because Docling and the encoders are
memory-heavy and this host has demonstrated native crashes under memory pressure. **Scale by adding
workers, not by raising concurrency** — raising it is how the worker starts dying and the backlog
gets worse. Verify each worker has a memory limit before adding replicas.

If the backlog is from a single pathological document, find it by `retry_count` and cancel it:

```bash
curl -X POST -H "Authorization: Bearer $CURATOR" \
  localhost:5173/api/v1/ingestion/jobs/$JOB/cancel
```

## Failed ingestion

**Symptoms.** Jobs in `FAILED` or `QUARANTINED`.

**Act.** Read `last_error_code` on the job. Retryable transport errors are retried automatically up
to `max_retries`; a parse or chunk failure is *not* retried blindly, because the same document will
fail the same way. Correct the source or the policy, then request an explicit reparse or rechunk —
both are audited actions with their own scopes.

A quarantined document is one that failed validation. Withdraw it rather than forcing it through.

## Bad index activation

**Symptoms.** Retrieval quality collapse after an activation; or corpus alignment failures.

**Act.** Vector semantics are never mutated in place, so the previous verified index still exists.
Reactivate it through the audited ingestion workflow and confirm the corpus alignment check passes
— retrieval fails closed if the chunk run, dense index and sparse index do not describe the same
corpus, so a half-rolled-back state refuses to serve rather than serving nonsense.

Rollback preserves corpus version, index identity, embedding model and revision, and retrieval
policy compatibility, because all four are recorded on the run.

## Bad configuration revision

**Symptoms.** Abstention rate or latency changes immediately after a settings change.

**Act.** History is immutable; you restore by **superseding**, never by editing.

```bash
curl -s -H "Authorization: Bearer $ADMIN" localhost:5173/api/v1/settings/history | jq '.[0:3]'
```

Read `effective_snapshot` from the last good revision, then preview and apply the prior value as a
new change. It takes effect for subsequent requests; in-flight requests keep the snapshot they
already resolved.

A pending rebuild proposal is abandoned the same way. Because a proposal never changed the
effective configuration, abandoning one has no effect on what is currently serving.

Note that no configuration change can have disabled a safety control — every safety invariant is
`IMMUTABLE`, and the editable gate thresholds sit at their floor, so a bad revision can only have
made the system *stricter* or degraded retrieval breadth.

## Model checksum failure

**Symptoms.** Readiness fails on the retrieval service or worker; a checksum mismatch is logged.

**Act. Do not bypass it.** A mismatch means the weights on disk are not the pinned revision, which
means the vector space may differ from the one the index was built in. Re-provision:

```bash
uv run python scripts/provision_embedding_model.py
uv run python scripts/provision_query_model.py
uv run python scripts/provision_reranker_model.py
```

If the mismatch persists, treat it as a supply-chain event: the cache has been modified. Production
refuses to start with `offline=false`, so the service cannot have silently downloaded a different
revision.

## Restore from backup

See [backup and recovery](architecture/backup-and-recovery.md) for the full procedure. In short:
restore PostgreSQL into a **disposable** target first, verify, then cut over.

```bash
uv run python scripts/verify_restore.py --database "$RESTORE_URL"
```

It refuses to run against the configured live database, checks the schema head, row counts,
referential integrity, object-key presence, the verified-answer rule and the configuration-history
immutability trigger. Never rehearse a restore against the live environment.

Recovery order is PostgreSQL → object store → model cache → Qdrant (rebuilt, not restored) →
Redis (empty is correct) → API and worker.

## Suspected credential compromise

1. Rotate at the source (identity provider, cloud provider, database).
2. Update the mounted secret file; the `_FILE` pattern means no image rebuild is needed.
3. Restart the API and worker.
4. Review `audit_events` for the affected actor and `configuration_revisions` for unexpected
   changes — history is immutable, so it cannot have been edited to hide activity.
5. If a provider key leaked, revoke it at the provider; this system never persists provider
   responses, so no additional cleanup of stored data is required.
