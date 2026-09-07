# Data retention and deletion

What this system stores, how long it keeps it, and — stated plainly — what it cannot yet delete.

**No legal or regulatory retention requirement is asserted here.** Retention periods are a
deployment's policy decision. This document describes the *control surface* that exists and the
gaps that remain, so a policy can be written against reality rather than against an assumption.

## What is stored

| Data | Location | Current behaviour |
|---|---|---|
| Original uploaded documents | Object store | Retained indefinitely. Authoritative evidence. |
| Document and version metadata | PostgreSQL | Retained indefinitely |
| Parses, pages, elements, tables, figures, formulas | PostgreSQL + object store | Retained; superseded runs kept for provenance |
| Chunks and BM25 postings | PostgreSQL | Retained per chunk run |
| Vectors | Qdrant | Retained per index run |
| Conversations, turns, verified answers, citations | PostgreSQL | Retained indefinitely |
| Configuration revisions | PostgreSQL | **Immutable and append-only.** Never deleted. |
| Audit events | PostgreSQL | Retained indefinitely |
| Evaluation artifacts | `docs/evals/` | Committed; regenerable |

## What is deliberately never stored

Recorded because absence is a control: prompts sent to providers, raw provider responses, drafts
that failed verification, verifier reasoning, request-scoped EvidenceSets, and any credential
value. A verified answer stores the citation text it was verified against rather than re-resolving
it, so a later re-parse cannot silently change what a stored answer appears to cite.

## Deletion today

`document:manage` allows a document to be withdrawn from the active corpus. **This is a state
change, not an erasure.** After it:

* The document no longer participates in retrieval.
* Its rows, artifacts, vectors, postings and original object remain.

That is the honest description. It is adequate for correcting a mistaken upload; **it is not a
right-to-erasure implementation**, and it must not be represented as one.

## What a complete deletion workflow would require

Deletion has to reach every derived surface, or content remains recoverable through a path nobody
was watching. The full set:

1. Object store — original plus every derived artifact (page previews, figure crops).
2. PostgreSQL — document, versions, parse runs, pages, elements, tables, figures, formulas, chunks,
   questions, BM25 postings, embedding run metadata.
3. Qdrant — every point for the document's chunks, across every index run that included it.
4. Any cached artifact.
5. Conversations that cited the document — **and here the requirements conflict.** A verified
   answer is bound by database CHECK constraints to the citations it was verified against.
   Deleting the source while keeping the answer would leave a verified answer whose evidence no
   longer exists; deleting the turn destroys a user's history. Which is correct is a policy
   decision, not a technical one, and it must be decided before the workflow is built.

**None of this is implemented.** Stating the design without the code is the point: it makes the gap
reviewable rather than invisible.

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
* No complete deletion workflow; withdrawal is a state change only.
* No cryptographic erasure.
* The verified-answer-versus-source-deletion conflict is undecided.

These are **known operational limitations**, not production blockers for a deployment whose policy
does not yet require deletion — but they are blockers for one that does, and that determination
belongs to the deploying organisation.
