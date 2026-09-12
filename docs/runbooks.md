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

## Start a clean local testing workspace (development only)

**Problem.** The local development tenant accumulated ~183 synthetic fixtures during M0–M12
("Browser fixture…", "Parse fixture…", "M8 synthetic verification smoke…"). They are useful
engineering evidence and should not be mixed with a real testing corpus.

**Solution — an additional tenant, not a deletion.** `MEDRAG_DEV_PRINCIPALS` is a list of
credentials that each carry their own `tenant_id`, and `ensure_actor` creates the tenant and user
rows idempotently on the first authenticated request. A clean Library therefore needs no migration,
no deletion and no new endpoint — only two more credentials pointing at an unused tenant id.

```bash
uv run python scripts/create_clean_dev_workspace.py            # dry run; shows the plan
uv run python scripts/create_clean_dev_workspace.py --apply
docker compose --profile app --profile workers up -d --force-recreate api
```

Then sign in with the new **curator** key from `.local/dev-access.txt` (git-ignored).

**Why this rather than deleting the fixtures.** Deletion is the one workflow this repository
deliberately has not built: a verified answer is bound by database CHECK constraints to the
citations it was verified against, so erasing a source while keeping the answer leaves verified text
with no evidence behind it. See [retention](architecture/retention.md). Adding a tenant sidesteps
that question entirely instead of answering it by accident, and it is instantly reversible — the old
workspace is reachable again by pasting its original key.

**Safety.** The script defaults to a dry run, refuses when `MEDRAG_ENVIRONMENT=production`, refuses
when `MEDRAG_AUTH__MODE` is not `development`, backs `.env` up before writing, preserves every
existing credential, prints only a four-character key fingerprint rather than a token, and deletes
nothing — no document, object, vector, index or audit record is touched.

**Switching back.** Sign out (or refresh the page) and paste the other workspace's key.
`--list` shows every configured workspace with its document count and key fingerprints.

**Expected on an empty workspace.** Asking a question before uploading anything returns
`FAILED` with reason `RETRIEVAL_CORPUS_EMPTY`. That is the M5 fail-closed behaviour for a tenant
with no indexed corpus, not a fault; it resolves once a document reaches `RETRIEVAL_READY`.

## Large PDF uploads

**Limits.** One PDF per HTTP request; selecting several uploads them one after another. The
application accepts **512 MiB per file** (`MEDRAG_INGESTION__MAX_UPLOAD_BYTES`) and the proxy
allows **520 MiB** (`MEDRAG_MAX_UPLOAD_MB`), so the application is what refuses an oversized file
and the user gets a typed error rather than the proxy's HTML page.

Both come from `compose.yaml`, and `scripts/production_preflight.py` fails when they disagree —
a proxy ceiling below the application's is what rejected a 153 MiB textbook with an opaque 413.

```bash
uv run python scripts/production_preflight.py   # upload.application_limit / upload.proxy_limit
docker compose exec frontend grep client_max_body_size /etc/nginx/conf.d/default.conf
```

**Raising the limit** means changing both, then rebuilding the frontend image and recreating the
API:

```bash
MEDRAG_INGESTION__MAX_UPLOAD_BYTES=805306368 MEDRAG_MAX_UPLOAD_MB=784 \
  docker compose --profile app --profile workers up -d --build
```

The application ceiling is 1 GiB by design. Uploading is bounded-memory, but *parsing* is not: a
document large enough to exhaust the worker must be refused at the door rather than accepted and
killed halfway through ingestion.

**After recreating the API container, restart the frontend.** nginx resolves `api` once at startup
and caches the address; a recreated API container gets a new IP and every proxied request returns
502 until the proxy re-resolves.

```bash
docker compose restart frontend
```

**Timeouts that matter for a large book**, in the order they apply:

| Bound | Setting | Default |
|---|---|---|
| Receiving the body | `MEDRAG_INGESTION__UPLOAD_TIMEOUT_SECONDS` | 1800 s |
| Proxy read/send | `MEDRAG_UPLOAD_TIMEOUT_SECONDS` | 1800 s |
| Structural validation | base + per-MiB, capped | 30 s + 0.6 s/MiB, max 600 s |
| One Docling call (a window, or a short whole document) | `MEDRAG_PARSING__TIMEOUT_SECONDS` | 900 s |
| Whole document, every window together | base + per-page, capped | 900 s + 12 s/page, max 14400 s |
| Celery soft / hard | `MEDRAG_PARSING__TASK_SOFT_TIMEOUT_SECONDS` / `__TASK_TIMEOUT_SECONDS` | 15600 s / 15900 s |

Validation scales with size because a flat 20 s budget timed out on a real 153 MiB book while
still being the right budget for a 1 MiB leaflet. The parse budget scales with **pages** for the
same reason: a 932-page textbook measured ~5 s/page, so it is over an hour of work, and no
constant serves both it and a one-page leaflet. One call < whole document < soft task limit <
hard task limit — enforced by a validator, so a document fails with a diagnosable parse error
rather than being killed by the worker with the reason lost. The soft limit clears the document
budget *plus* one call, because the budget is checked between windows and a parse already over
budget still finishes the window it is in.

These ceilings are large on purpose. A worker slot genuinely held for hours by one book is the
cost of ingesting one; if that is unacceptable for a deployment, lower
`MEDRAG_PARSING__MAX_PAGES` so oversized books are refused at the door rather than lowering the
budget so they fail halfway through.

**One large book cannot starve the queue indefinitely**, but it does occupy the single worker slot
while it parses: `MEDRAG_PARSING__MAX_CONCURRENCY` is 1 because the models are memory-heavy. Scale
by adding workers, never by raising concurrency.

## Qualifying a larger book than the one already run

The largest document actually taken end to end is **153 MiB / 932 pages**: 932 pages parsed,
18,888 elements, peak worker memory 4.74 GiB, 4042 s of parsing. Nothing larger has been run, and
the application's 512 MiB upload ceiling is **not** a statement that a 512 MiB book will parse.

Qualify one size at a time, and never by raising RAM first:

1. **Predict from the measurements.** Parsing cost scales with pages, not megabytes. Take the page
   count, not the file size: 25-page windows cost ~5 s and ~1.3 GiB settled each on this hardware,
   so pages x 5 s is the parse time to expect and peak memory should *not* move with page count.
2. **Check the budgets fit.** `document_timeout_for(pages)` must exceed the predicted time, and
   `MEDRAG_PARSING__MAX_PAGES` must not refuse the document. Both are reported by
   `scripts/production_preflight.py`.
3. **Record the baseline** before starting, so the peak is attributable:

   ```bash
   docker compose up -d --force-recreate worker   # resets the cgroup high-water mark
   docker compose exec worker cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes
   ```

4. **Watch it, do not just wait.** Peak memory must stay flat across windows; if it climbs with
   page count, stop and investigate rather than adding RAM.

   ```bash
   docker compose logs -f worker | grep PARSE_WINDOW_COMPLETED
   watch -n 30 'docker compose exec -T worker grep "^rss " /sys/fs/cgroup/memory/memory.stat'
   ```

5. **One size at a time.** Qualify ~300 MB before ~500 MB, and record pages, elements, duration and
   peak memory for each. A size is supported when a real book of that size has completed, not when
   a host with more memory has been provisioned.

Downstream of parsing is not qualified by this procedure. Chunking and embedding a document with
tens of thousands of elements has its own cost and has not been measured at that scale.

## A large book is killed while parsing (SIGKILL / WorkerLostError)

**Symptom.** `ForkPoolWorker-N exited with signal 9 (SIGKILL)` and `WorkerLostError` in the worker
log, the container itself reporting `OOMKilled=false`, and the document sitting in `PARSING` in
the Library. The kernel killed the *child* process, so the container survived and nothing in the
application layer observed a failure.

**Confirm it was memory**, from the host:

```bash
docker compose exec worker cat /sys/fs/cgroup/memory.peak       # or memory.max_usage_in_bytes
docker compose exec worker grep oom_kill /sys/fs/cgroup/memory.events
docker stats --no-stream worker
```

**Cause.** Docling retains per-page state for the life of one `convert()` call, so a
single-call conversion grows linearly with page count — measured at **44 MiB per page on real
textbook content**. A 932-page medical textbook reached 6.2 GB before the OOM killer intervened.

**Fix.** Long documents are converted in page windows
(`MEDRAG_PARSING__PAGE_WINDOW_SIZE`, default 25), which holds peak RSS flat regardless of book
length. If a book still dies, lower the window size before adding RAM:

```bash
MEDRAG_PARSING__PAGE_WINDOW_SIZE=10 docker compose --profile app --profile workers up -d worker
```

Windowing is on by default. `0` disables it and restores whole-document conversion, which is only
appropriate for a corpus of short documents.

**Recovery of a stuck job.** The dispatcher's reaper moves a run whose lease has expired to
`FAILED` with `PARSER_LEASE_EXPIRED`, and the job becomes retryable — no manual database edit is
needed. During a windowed parse the lease is renewed between windows
(`PARSE_WINDOW_COMPLETED` in the worker log), so the lease reflects liveness rather than document
length, and a genuinely dead worker is detected within `MEDRAG_PARSING__LEASE_SECONDS` however
long the book was. Watch progress live:

```bash
docker compose logs -f worker | grep PARSE_WINDOW_COMPLETED
```
