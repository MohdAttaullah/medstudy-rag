# ADR-026: Permanent document deletion, and what happens to answers that cited it

Status: accepted. Post-M12 product work; no milestone was created. Adds a migration
(`document_purge`), one table, one column, one permission and one endpoint. Archive is unchanged.

## Context

Until now the only way to take a document out of use was **archive**: a state change that
withdraws it from retrieval and keeps every byte — the original, every parse artifact, every chunk,
every vector. `docs/architecture/retention.md` said so plainly and listed the missing deletion
workflow as a known gap, blocked on one undecided question:

> A verified answer is bound to the citations it was verified against. Deleting the source while
> keeping the answer would leave a verified answer whose evidence no longer exists; deleting the
> turn destroys a user's history.

Three further facts made deletion more than a cascade:

- **Three stores, no shared transaction.** PostgreSQL, the object store (versioned — a plain
  `DELETE` leaves every earlier version behind a delete marker) and Qdrant.
- **Immutability guards.** Completed chunk, embedding and lexical datasets are protected by
  triggers (M3/M4/M5) that reject `DELETE`; stage events and validation findings were append-only
  via `m1_append_only`. Those guards are why a published dataset can be trusted, and they had to
  stay in force for every other purpose.
- **Workers hold leases.** A cancelled parse notices cancellation only at its next fence, so a
  worker could write an artifact *after* the cleanup and recreate content the user asked to remove.

## Decision

### 1. Answers stay; their citations lose the deleted content

A conversation turn that cited the document is **kept**, and its citations are kept as rows, but
each one is **redacted**: `cited_text` emptied, `document_title` replaced with "Deleted source",
page, element, span and artifact lists emptied, and `source_deleted_at` set. The foreign key from
`turn_citations.document_id` to `documents` is dropped so the citation can outlive its document.
The interface shows such a citation as **"Source deleted"** with no excerpt and no links.

Rejected alternatives:

- *Keep the excerpt so the citation stays readable.* That is keeping the deleted text. A user who
  deletes a document must not find its sentences preserved in someone's conversation.
- *Delete the turn.* Destroys another user's history to satisfy one deletion, and the answer text
  is that user's record of what they were told.
- *Re-verify or withdraw the answer.* The answer was verified when it was given; the record of that
  is accurate. What changes is that it can no longer be re-checked, and the card says exactly that.

### 2. Workflow: withdraw → external cleanup → purge

1. **Withdraw** (one transaction): archive the document and its versions, cancel every job through
   the state machine, write a `document_deletions` tombstone (`REQUESTED`), audit
   `DOCUMENT_DELETE_REQUESTED`. The document leaves retrieval here, before anything else can fail.
2. **External cleanup**: remove every object *version and delete marker* under
   `documents/{document_id}/`; delete every Qdrant point filtered by `tenant_id` **and**
   `document_id` in every collection the document's index runs used, then count to confirm zero.
   This runs *before* the relational purge because PostgreSQL is the manifest of what to clean.
3. **Purge** (one transaction): set a transaction-local marker
   `set_config('medrag.purge_document', <id>, true)`, delete rows children-first, redact citations,
   mark the tombstone `COMPLETED` with the counts removed, audit `DOCUMENT_DELETED`.

Any failure in steps 2–3 marks the tombstone `FAILED`, audits `DOCUMENT_DELETE_FAILED` with a code
(`STORAGE_DELETE_FAILED`, `VECTOR_INDEX_UNAVAILABLE`, `VECTOR_DELETE_FAILED`,
`VECTOR_DELETE_INCOMPLETE`, `DATABASE_PURGE_FAILED`) and returns `503 DOCUMENT_DELETE_INCOMPLETE`.
The document stays withdrawn and nothing is searchable; repeating the request resumes. Every step
is idempotent, and a request against a `COMPLETED` tombstone returns `204` without doing anything.

### 3. The guards admit exactly one document, for one transaction

The M3/M4/M5 dataset guards and a new `document_history_append_only` trigger (replacing
`m1_append_only` on stage events and on parse/index/lexical findings and parse review decisions)
allow `DELETE` only when the row belongs to the document named by `medrag.purge_document`. `UPDATE`
of history is still always refused. The setting is transaction-local, so it cannot leak into a
later statement on the same connection, and it names one document, so a purge cannot reach a
neighbour. **`audit_events` and `configuration_revisions` are not touched**: their triggers are
unchanged and they are never deleted.

### 4. Processing blocks deletion

Deletion is refused (`409 DOCUMENT_PROCESSING`) while any job for the document is non-terminal, or
while any parse, chunk, embedding or lexical run is active with a current lease (or, without a
lease, was touched in the last 15 minutes). The user cancels processing and waits for it to stop.
A concurrent second request within 15 minutes gets `409 DOCUMENT_DELETION_IN_PROGRESS`.

### 5. Authorization

New permission `document:delete`, held by **admin only** — not curator, not reader. Archive keeps
`document:manage`. The endpoint is `DELETE /api/v1/documents/{id}` with body
`{"confirm": "DELETE"}` (`400 DELETE_NOT_CONFIRMED` otherwise); the confirmation is enforced by the
server, not only by the dialog. Another tenant's document answers `404 DOCUMENT_NOT_FOUND`, the same
as one that never existed. `GET /documents/{id}/deletion-preview` returns the counts the dialog
shows (versions, pages, chunks, vectors, citing answers) and whether processing blocks it.

### 6. What remains after deletion

- The `document_deletions` tombstone: tenant, document id, who, when, attempts, last error code,
  and counts of what was removed. **No title, filename or content.**
- Audit events, as for every other action. Their details carry counts and codes, never content.
- Redacted citations, as above.

## Consequences

- Right-to-erasure requests for a document can be honoured without database surgery.
- The immutability guarantees of published datasets now have one narrow, audited exception.
- Downgrading the migration restores the citation foreign key and therefore refuses to run while
  any citation points at a deleted document, unless `MEDRAG_ALLOW_CONVERSATION_LOSS=1` is set, in
  which case those orphan citations are removed. That is a deliberate, explicit loss.
- Answers whose every citation was deleted remain readable but uncheckable. The interface says so.
- Backups taken before a deletion still contain the document. Erasure from backups is a backup
  retention policy, not something this workflow can do (see `retention.md`).
- Cryptographic erasure was not adopted; it remains the recommended path if provable deletion
  across every copy is required.

## Verification

`backend/tests/test_document_delete_integration.py` (22 tests, real PostgreSQL, MinIO and Qdrant)
proves: reader and curator `403`; another tenant `404` with the document untouched; confirmation
required; processing `409`; every row, object version, delete marker and vector removed; the
document absent from the library and from retrieval; a neighbour document in the same tenant intact;
citing answers kept with redacted, `source_deleted` citations; tombstone and audit without the title;
idempotent repeat; partial failure leaves it withdrawn and a retry completes; the guards still
refuse deletes and history updates outside a purge; a purge of one document cannot delete another's
rows; audit events cannot be deleted; the purge marker does not outlive its transaction.
