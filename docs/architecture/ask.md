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

Progress stages only. Answer text becomes visible only after verification completes; streaming
draft tokens would put unverified medical text in front of a reader, and safety takes priority over
perceived responsiveness.

`POST /ask` answers with one JSON response by default and, for a caller sending
`Accept: text/event-stream`, with Server-Sent Events: `stage` frames while the request runs, then a
single `result` frame carrying exactly the JSON body the other form returns. `AskConfig.
stream_progress_stages` enables the negotiated form. Nothing else changes — the same service call,
the same pipeline, the same response model — so a client that does not negotiate keeps the contract
it was written against, and the frontend handles both shapes.

Seven stages, each announced by the code that performs it:

| Stage | Emitted by | Covers |
|---|---|---|
| `PREPARING` | `AskService` | scope and intent, read from the question alone |
| `RETRIEVAL` | `EvidenceService` | the M5 lanes, fusion and candidate hydration |
| `RERANK` | `EvidenceService` | the cross-encoder |
| `EVIDENCE` | `EvidenceService` → `GenerationService` | expansion and assembly, closing at the sufficiency gate |
| `GENERATION` | `GenerationService` | the grounded draft |
| `VERIFICATION` | `VerificationService` | claim checking, repair and the contradiction scan |
| `FINALIZE` | `AskService` | recording the turn and its citations |

A stage is `RUNNING` when that code begins and `COMPLETED` when it returns; `SKIPPED` when the
pipeline did not reach it — an out-of-scope question skips five, an insufficient-evidence gate skips
generation and verification — and `FAILED` when it raised. A stage still running when the request
ends is reported failed rather than left spinning. `PENDING` is never emitted: a stage nobody
announced has not started, which the client renders as such.

An event carries a request id, a stage, a state, a sequence number and timings. **It has no field
that could hold content** — no draft, no claim, no verdict, no rationale, no evidence text — so the
channel cannot widen what M8 decided a reader may see. Concluding against a draft is not a stage
failure: `VERIFICATION` completes and the outcome carries the refusal.

The reporter reaches the orchestrators through a `ContextVar`, so no stage signature changed, and
`run_in_threadpool` copies that context into the worker thread the synchronous M5/M6 stages run in.
Progress exists only on the asking connection: there is no id to fetch somebody else's progress by,
and no shared store to read it from. A refusal that the JSON form answers with 4xx arrives on the
stream as a typed `error` frame, because the status line is already sent by then.

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

`GET /conversations` lists the signed-in principal's conversations and `GET /conversations/{id}`
returns one with its turns, both scoped server-side to the authenticated tenant and user — a
foreign id is *not found*, which is what an id that never existed returns too. The Ask page reads
both: the open conversation is held for the life of the sign-in and mirrored into the address, so
navigating away and back reopens it and a reload restores it, while **only the id** is held client
side and the turns always come from the server. Signing out clears it, so the next principal starts
with nothing selected. New conversation is the only way to leave one.

## Stage timings

`stages` reports one row per reader-facing stage, summing the measurements that share a label.
Subtotals are excluded because they double-count their own parts, and `Total` is measured at the
request boundary rather than summed from the rows — so whatever the stages do not account for stays
visible as the difference instead of disappearing. The detailed breakdown is shown only to a
principal holding `retrieval:search`; a reader sees the total.

The boundary is the whole request: the clock starts before the conversation-ownership check and the
idempotency replay lookup, both of which a caller waits for, and stops after the turn is recorded —
`persistence_ms` is its own row. M8's own measurements (claim extraction, claim verification, the
contradiction scan and any repair) are merged from the verification report, so the largest stage of
a long request appears as a row and not only inside the total. One limitation: a repaired draft is
verified twice and the per-round keys are overwritten, so those rows describe the final round while
the total still covers both.

The downgrade refuses while conversations exist; `MEDRAG_ALLOW_CONVERSATION_LOSS=1` acknowledges the
loss deliberately, which is what the test harness sets on its throwaway schema.

## Source figures

A verified answer may display figures from the documents it cites. They are **supplementary source
material, never evidence**: nothing interprets an image anywhere in this system, no claim may rest
on one, and `VISUAL_INTERPRETATION_REQUIRED` still refuses any claim that would need pixels read.

A figure appears only when the answer's own cited evidence links to it, by one of two links that
can be checked by looking:

| `linked_by` | The link |
|---|---|
| `CITED_EVIDENCE` | a citation *is* that figure — a `FIGURE_CONTEXT` chunk carries its artifact id |
| `CITED_TEXT_REFERENCE` | the citation's stored, verified text names the figure, and a figure in the same parse run declares that label in its caption |

Sharing a page is not a link. Sharing a document is not a link. Subject-matter similarity is not a
link. `app/services/figures.py` reads the labels a passage actually writes — "( Fig. 1.2 )",
"( Figs. 1.2-1.4 )", "( Figs. 1.2 and 1.7 )" — expands a range only across one major number, and
matches them against captions that declare the same label. A figure with no stored image is never
offered, because a link that 404s is worse than no link. At most six are listed, in page order.

`AskResponse.figures` and the stored turn view are resolved by the same function from the same
stored citations, so an answer that showed a figure still shows it when the conversation is read
back. Nothing about figures is persisted: the citations already are, and the link is derived.

Images are served by the existing M2 route —
`GET /documents/{id}/versions/{v}/parse-runs/{run}/figures/{figure}/image` — under `document:read`,
scoped to the caller's tenant, with the figure required to belong to the parse run named in the
path. No object-store key, bucket or signed URL ever reaches a client. Because that route needs an
`Authorization` header and an `<img src>` cannot send one, the browser fetches the bytes like any
other request and renders them from an object URL.

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
