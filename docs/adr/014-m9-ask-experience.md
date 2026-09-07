# ADR-014: One endpoint may answer, and only behind a verified pass

Status: Accepted for M9. Connects the M5–M8 chain to a user-facing surface without altering any stage in it. Introduces the first new durable state since M1.

## The display rule lives in the contract, not in the caller

`AskResponse` carries an answer if and only if its outcome is `VERIFIED`, `verified` agrees with both, and a non-verified outcome carries no claims, citations or sources. A model validator refuses every other combination, so the shape that would let an interface render a rejected draft —

```json
{"outcome": "UNVERIFIED", "draft_answer": "Take 50 mg..."}
```

— cannot be constructed at all. That matters more than a rule written down somewhere: a future route, serializer or refactor cannot leak a draft by forgetting, because there is no valid object to leak it in. The same validator guards the stored-turn view, and two database CHECK constraints put the rule under the application as well: `answer_text` exists exactly when the outcome is `VERIFIED`, and `verified` matches the outcome. A failed draft cannot be written into the column a verified answer is read from.

The response has no field for the M7 draft, the sufficiency signals, the verifier's reasoning or any provider internals. Those exist and authorized reviewers can see them on the inspector routes; they have no place in the surface a reader looks at.

## M9 decides nothing about answerability

`AskService` calls the existing pipeline, reads the outcome M7 and M8 already reached, records the turn and shapes a response. It contains no notion of what makes evidence sufficient or a claim supported. Duplicating any part of that would create a second place for the answer rule to live, and the second place is the one that gets it wrong. `RETRIEVAL_READY` still means the corpus may participate in retrieval; answerability stays request-specific and is never promoted to a global state.

Four refusals are distinguished because they call for four different responses from a reader: the corpus lacks evidence, the sources disagree, the draft failed checking, or the service broke. Collapsing them into one generic failure would tell a user nothing and would let an infrastructure outage masquerade as a statement about the evidence.

## Enablement is per-contract, not a global switch

Every M5–M8 response keeps `answering_enabled: Literal[False]` and returns exactly what it always did; no earlier endpoint became an answering endpoint. `AskResponse` pins the flag `Literal[True]`, which is a statement about *this* contract — this response may contain an answer — rather than a feature flag that turns answering on somewhere. `AskConfig.requires_verified_pass` is `Literal[True]` and `stream_answer_tokens` is `Literal[False]`, so no configuration value can relax either. A boolean that could would be the single point at which the whole safety chain becomes optional.

Streaming, when it arrives, may carry stage names only. Streaming draft tokens would put unverified medical text in front of a reader, and no later correction takes that back. The page shows bounded stage labels while it waits and reveals the answer atomically after verification.

## Authorization, and one thing it broke

Asking is a reading capability: `reader` gains `ask:submit` and `conversation:read` and nothing else. The inspector scopes stay separate, so a reader who may ask a question still cannot see a draft, a lane score or a verifier verdict.

That created a real problem. Retrieval, drafting and verification each run for two kinds of caller — a reviewer using the diagnostic endpoints, and an ordinary reader asking a question — and their service-level checks demanded the reviewer's scopes. Relaxing them to `require_any(<stage scope>, "ask:submit")` fixed the reader's path and immediately opened `/retrieval/search` to readers, because that route had no check of its own and relied entirely on the service. The route now enforces its own scope, which is what its module docstring had claimed all along. Defence in depth only works when both depths exist.

Conversation ownership comes from the authenticated principal, never from the id in the URL. Every query filters on the caller's tenant, so another tenant's conversation is *not found* rather than found and refused — a distinguishable "forbidden" would confirm the id is real to someone probing. Ownership is also checked before the pipeline runs, so an id that is not the caller's costs no provider call.

## Persistence, and what is deliberately not stored

A conversation the user returns to must outlive the request, so this is the first milestone since M1 to add durable state: conversations, turns and the citations behind a verified answer. Stored are the question, the outcome, the answer *only when verified*, declared reason codes, model identity and timings. Not stored: prompts, provider responses, drafts that failed verification, verifier reasoning and request-scoped EvidenceSets. None of those is something a user needs to re-read, and each is something a store should not accumulate.

Citation text is stored rather than re-resolved at read time. A verified answer was verified against those exact words; if the document were later re-parsed, re-resolution would quietly change what a stored answer appears to cite, which is the drift provenance exists to prevent. The identifiers alongside it still resolve to the live document.

The downgrade refuses while conversations exist. Every other table here can be rebuilt from its source document; a user's question history cannot, and dropping it silently to move a schema pointer would be the one irreversible thing a migration in this repository does. `MEDRAG_ALLOW_CONVERSATION_LOSS=1` acknowledges the loss deliberately, which is what the integration harness sets on the throwaway schema it created seconds earlier.

## Citations open the real source

No new document routes were added. The M2 parse API already streams page previews, elements, tables, figures and formulas under `document:read` with tenant scope resolved server-side, so a citation deep-links there and the browser never holds an object-store URL of its own. The link targets the page the cited *span* is on rather than the citation's first page, because a citation spanning two pages whose region sits on the second must open the second or the highlight points at nothing.

Regions come from M2 provenance and are shown when recorded and stated as absent when not; no geometry is invented. Table citations carry their canonical row and header identity, formula citations state that the notation is the document's own, and figure citations distinguish an available original crop from an unavailable one. Assessment sources are labelled as assessment material wherever they appear, so a question bank cannot look like a reference.

## Consequences

Coverage remains what M7 and M8 make it. On the ingested synthetic corpus most questions still abstain, and the live end-to-end pass required a document written in coherent prose — the parsing fixtures are shaped to exercise the parser, and a verifier correctly refuses claims drawn from their fragmentary text. Latency is dominated by the provider: roughly 13.7 s end to end in the live run, of which generation and verification are almost all. None of that is a production SLO, and no threshold anywhere in M9 is calibrated.
