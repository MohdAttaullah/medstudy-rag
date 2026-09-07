# M9 Ask experience

`POST /api/v1/ask` requires `ask:submit`. It runs the whole verified pipeline and returns either an
answer or a typed refusal that says which kind it was.

```
question → M5 retrieval → M6 rerank + evidence → M7 gate → M7 draft
        → M8 claim verification → PASS → verified answer + citations
        ↘ any other outcome → typed refusal, no answer text anywhere
```

M9 adds no decision. `AskService` reads the outcome M7 and M8 already reached, records the turn and
shapes the response. `RETRIEVAL_READY` still means the corpus may participate in retrieval;
answerability stays request-specific.

## The display rule

`AskResponse` carries an `answer` **if and only if** `outcome == "VERIFIED"`, `verified` agrees with
both, and a non-verified outcome carries no claims, citations or sources. A model validator refuses
every other combination, so an unverified draft cannot be placed in a response at all. The stored
turn view enforces the same rule on read, and two database CHECK constraints enforce it underneath:
`answer_text` exists exactly when the outcome is `VERIFIED`, and `verified` matches the outcome.

The response has no field for the draft, the sufficiency signals, the verifier's reasoning or any
provider internals. Authorized reviewers see those on the M5–M8 inspector routes.

## Outcomes

| Outcome | Meaning |
|---|---|
| `VERIFIED` | Every material claim was checked against the sources cited with it. |
| `INSUFFICIENT_EVIDENCE` | The indexed sources do not support an answer. Nothing is filled in from model knowledge. |
| `CONFLICTING_EVIDENCE` | The sources disagree and the disagreement is unresolved. No source is chosen. |
| `UNVERIFIED` | Evidence existed; the draft failed verification. The draft is not shown. |
| `FAILED` | A technical failure. Explicitly not a statement about the evidence. |

Each renders as its own state with its own explanation. No confidence figure exists anywhere —
an answer is verified against its sources or it is not shown.

## Enablement

Every M5–M8 response keeps `answering_enabled: Literal[False]` and behaves exactly as before; no
earlier endpoint became an answering endpoint. `AskResponse` pins the flag `Literal[True]` as a
statement about that contract, not a global switch. `AskConfig.requires_verified_pass` is
`Literal[True]` and `stream_answer_tokens` is `Literal[False]`; no configuration relaxes either.

## Streaming

Progress stages only, and bounded ones: *Searching sources*, *Reranking evidence*, *Checking
evidence sufficiency*, *Drafting from evidence*, *Verifying claims*. Answer text becomes visible
only after verification completes. Streaming draft tokens would put unverified medical text in
front of a reader; safety takes priority over perceived responsiveness.

## Request boundary

The client sends a question, an optional conversation to continue, an optional idempotency key and
permitted M5 filters. `extra="forbid"` rejects a tenant id, evidence ids, `verified`, an outcome, an
answer, a provider, a model, a prompt or a verification result with 422. Tenant and corpus identity
are server-owned.

## Authorization

`reader` holds `ask:submit` and `conversation:read` — and nothing else. Inspector scopes
(`retrieval:search`, `generation:draft`, `generation:verify`) stay separate, so a reader who may ask
still cannot see a draft, a lane score or a verifier verdict.

Retrieval, drafting and verification run for both kinds of caller, so their service-level checks use
`require_any(<stage scope>, "ask:submit")`. Each diagnostic route therefore enforces its own scope
at the boundary; relying on the service alone is what briefly opened `/retrieval/search` to readers
during development.

Conversation ownership comes from the authenticated principal, never the URL. Every query filters on
the caller's tenant, so another tenant's id is *not found* rather than found and refused. Ownership
is checked before the pipeline runs, so a foreign id costs no provider call.

## Conversations

`conversations`, `conversation_turns`, `turn_citations` — all tenant-scoped with composite keys.
Stored: question, outcome, answer (only when verified), declared reason codes, model identity,
timings and the citations behind a verified answer. **Not stored**: prompts, provider responses,
failed drafts, verifier reasoning, EvidenceSets.

Citation text is stored rather than re-resolved: the answer was verified against those exact words,
and a later re-parse must not silently change what a stored answer appears to cite.

One submission is one turn. A retry reusing the idempotency key returns the stored turn instead of
spending another provider call.

The downgrade refuses while conversations exist; `MEDRAG_ALLOW_CONVERSATION_LOSS=1` acknowledges the
loss deliberately, which is what the test harness sets on its throwaway schema.

## Citations and the source viewer

No new document routes. Citations deep-link into the existing M2 parse inspector, which streams page
previews, elements, tables, figures and formulas under `document:read` with tenant scope resolved
server-side. The browser never holds an object-store URL.

The link targets the page the cited **span** is on, not merely the citation's first page. Regions come
from M2 provenance, are shown when recorded and stated as absent when not; no geometry is invented.
Table citations carry canonical row and header identity; formula citations state the notation is the
document's own; figure citations distinguish an available original crop from an unavailable one.
Assessment sources are labelled as assessment material wherever they appear.

The source list is built only from citations that supported the answer. Retrieval candidates that
supported nothing stay in the authorized inspector.

## Telemetry

`ask_outcomes_total{outcome}` counts outcomes. Logs carry the correlation id, the outcome and the
declared reason codes. The question, the answer, the sources and every key stay out of logs and
metric labels, as they have since M5.

See [ADR-014](../adr/014-m9-ask-experience.md), [sufficiency](sufficiency.md),
[generation](generation.md) and [verification](verification.md).
