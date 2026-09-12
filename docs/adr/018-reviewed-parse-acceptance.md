# ADR-018 — Reviewed acceptance of a flagged parse

Status: accepted. Post-M12 remediation; no milestone was created.

## Context

ADR-007 §Decision 3 made a successful parser run insufficient: a deterministic rule layer decides
`PASS`, `PASS_WITH_WARNINGS`, `NEEDS_REVIEW` or `FAIL`, and only the first two reach
`READY_FOR_CHUNKING`. It recorded the cost honestly — *"a `NEEDS_REVIEW` document needs an
operator. There is no review-approval workflow yet — the operator path is reparse or cancel."*

A real 153 MiB, 932-page medical textbook made that cost concrete. Two pages of 932 raised
`PAGE_CONTENT_LOST`, both investigated and found to be provenance-precision limits rather than
content loss: page 44 omits only a decorative section marker, and page 517 carries the tail of a
paragraph Docling anchors to page 516, where its text is retained in full. A curator could see
exactly that in the parse inspector and had nowhere to put the conclusion. Worse, the Operations
view offered Rechunk and Re-embed for such a job; both fail with `CHUNK_SOURCE_PARSE_NOT_READY`
*after* spending one unit of the bounded retry budget.

## Decision — acceptance is a separate immutable fact, not a corrected verdict

A flagged parse keeps `validation_result = NEEDS_REVIEW` permanently and keeps every
`parse_validation_findings` row. An authorized curator's acceptance is recorded as an append-only
`parse_review_decisions` row bound by foreign key to one exact parse run, and the run moves to a
new status, `ParseRunStatus.REVIEWED_ACCEPTED`.

*Why not rewrite the result to `PASS_WITH_WARNINGS`.* One line of code, no new table — and it
destroys the only record that automated validation objected and a human overrode it. Every later
reader would see a parse that passed.

*Why not reuse `SUCCEEDED`.* It asserts the parse passed, which is false. Keeping the statuses
distinct is what lets the database guards, the chunking predicate, the inspector and any future
audit tell automatic success from accepted-after-review.

*Why per-run rather than per-document.* A decision that outlived its parse would silently bless
output nobody reviewed. The foreign key makes carry-over unrepresentable: a reparse creates a new
run with no decision. `findings_digest` pins the exact finding set reviewed, because the
append-only trigger blocks UPDATE and DELETE but not INSERT.

*Why no retry is consumed.* The bounded budget exists to stop reprocessing loops. Acceptance
creates no run and reprocesses nothing — it admits the dataset the curator just examined — so
charging it a retry would penalise review and could exhaust the budget of a document that still
needs a genuine reparse.

## Consequences

Five enforcement points moved together, because eligibility was expressed in four different places
in the database and two in Python:

| Guard | Change |
|---|---|
| `ck_parse_runs_parse_run_status` | vocabulary and column width admit `REVIEWED_ACCEPTED` |
| `ck_parse_runs_active_run_usable` (was `…_succeeded`) | an active run may be `SUCCEEDED` **or** `REVIEWED_ACCEPTED` |
| `m1_job_guard` | a `NEEDS_REVIEW → READY_FOR_CHUNKING` edge with **unchanged** `retry_count`, conditioned on an active `REVIEWED_ACCEPTED` run existing |
| `m3_run_guard` INSERT branch | chunk-run source may be `SUCCEEDED` or `REVIEWED_ACCEPTED` |
| `m3_run_guard` activation branch | keyed on `validation_result`; now also admits `status = 'REVIEWED_ACCEPTED'` |
| `parse_review_guard` (new) | `REVIEWED_ACCEPTED` only from `FAILED` + `NEEDS_REVIEW`, only with an ACCEPT decision for that run; `validation_result` immutable once written |

The activation branch is the subtle one. Because a reviewed parse keeps `NEEDS_REVIEW` forever, a
guard that reasons from `validation_result` alone would let chunking run to completion and then
refuse to activate the dataset it produced. Anything added later that gates on "is this parse
usable" must consult `status` as well; the `ParseRunStatus` docstring says so.

*Cost.* Eligibility is now a two-clause predicate rather than one, and the rule lives in
`usable_parse` so the service and repository cannot drift. Downgrading past this revision refuses
while any reviewed acceptance exists, because the state cannot be represented without it.

*Rejected.* A job-level `REVIEWED_ACCEPTED` status: it would widen a 22-character vocabulary
across three tables and rewrite the whole transition guard to express something the parse run
already records. A deferrable composite foreign key instead of `parse_review_guard`: declarative,
but it makes the two tables circularly dependent using a pattern this repository has never used,
where triggers are the established idiom.

*Not built.* `REJECT` decisions, bulk acceptance, and per-page or per-finding acceptance. The
validation rule itself is unchanged: `min_chars_per_page` remains an absolute floor, so partial
loss on a dense page stays invisible to it. That is a validation-design question, not a
review-workflow one.
