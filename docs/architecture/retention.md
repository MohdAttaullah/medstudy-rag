# Data retention and deletion

What this system stores, how long it keeps it, how a document is deleted, and — stated plainly —
what it still cannot delete.

**No legal or regulatory retention requirement is asserted here.** Retention periods are a
deployment's policy decision. This document describes the *control surface* that exists and the
gaps that remain, so a policy can be written against reality rather than against an assumption.

## What is stored

| Data | Location | Current behaviour |
|---|---|---|
| Original uploaded documents | Object store | Retained until permanently deleted (ADR-026). Authoritative evidence. |
| Document and version metadata | PostgreSQL | Retained indefinitely |
| Parses, pages, elements, tables, figures, formulas | PostgreSQL + object store | Retained; superseded runs kept for provenance |
| Chunks and BM25 postings | PostgreSQL | Retained per chunk run |
| Vectors | Qdrant | Retained per index run |
| Conversations, turns, verified answers, citations | PostgreSQL | Retained indefinitely; citations to a deleted document are redacted |
| Deletion tombstones | PostgreSQL | Retained; no title or content |
| Configuration revisions | PostgreSQL | **Immutable and append-only.** Never deleted. |
| Audit events | PostgreSQL | Retained indefinitely |
| Evaluation artifacts | `docs/evals/` | Committed; regenerable |

## What is deliberately never stored

Recorded because absence is a control: prompts sent to providers, raw provider responses, drafts
that failed verification, verifier reasoning, request-scoped EvidenceSets, and any credential
value. A verified answer stores the citation text it was verified against rather than re-resolving
it, so a later re-parse cannot silently change what a stored answer appears to cite.

## Archive and permanent deletion

There are two ways to take a document out of use, and they are deliberately different.

**Archive** (`document:manage`) is a state change, not an erasure. The document leaves retrieval;
its rows, artifacts, vectors, postings and original object remain. It is the right tool for a
mistaken upload or a superseded edition.

**Permanent deletion** (`document:delete`, admin only — ADR-026) erases the document and everything
derived from it:

1. Object store — every object *version and delete marker* under `documents/{document_id}/`: the
   original, page previews and figure crops. The bucket is versioned, so removing only the current
   version would not be an erasure.
2. PostgreSQL — the document, versions, upload intents, jobs and their stage events, parse runs and
   everything under them, review decisions and findings, chunk runs, chunks and their links,
   questions, embeddings and embedding/index runs, the lexical index and its postings, outbox rows.
3. Qdrant — every point carrying the document's tenant and document id, in every collection its
   index runs used, confirmed by a count of zero.

The workflow is withdraw → external cleanup → purge, and is resumable: if a step fails, the
document stays withdrawn from search and repeating the request completes it. A document still
being processed cannot be deleted. Deletion is refused across tenants with the same `404` as a
document that does not exist.

**Conversations that cited a deleted document** (the conflict this document previously left
undecided) keep their turns and answer text. Each citation to the deleted document is redacted —
excerpt, title, pages, spans and artifacts removed — and marked `source_deleted`; the interface
shows "Source deleted". The deleted text is not kept anywhere to keep a citation readable.

**What remains** is contentless: a `document_deletions` tombstone (document id, who, when, attempts,
counts of what was removed — no title or filename) and the audit events `DOCUMENT_DELETE_REQUESTED`,
`DOCUMENT_DELETED` and `DOCUMENT_DELETE_FAILED`.

**Not reached by deletion:** backups taken before it, and any copy outside this system (a
downloaded original, a screenshot). Removing a document from backups is a backup-retention policy.

## Audit and configuration history are exempt

Configuration revisions are protected by a database trigger that rejects `UPDATE` and `DELETE`, and
audit events record who did what. **Neither should be included in a deletion workflow** without an
explicit, recorded decision: they are the evidence that the system behaved as claimed, and a
deletion feature that quietly removed them would destroy exactly the record an investigation needs.

## Cryptographic erasure

Not implemented. Per-tenant or per-document encryption keys, discarded on deletion, would make
erasure verifiable without chasing every derived copy. This is the recommended approach if the
deployment's policy requires provable deletion; it is a substantial change and was not attempted
under a hardening milestone.

## Summary of gaps

* No configurable retention period for any data class.
* No scheduled purge.
* Deletion reaches the live stores, not backups taken before it.
* No cryptographic erasure.

These are **known operational limitations**, not production blockers for a deployment whose policy
does not yet require deletion — but they are blockers for one that does, and that determination
belongs to the deploying organisation.
