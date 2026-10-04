# ADR-027: Retrieval units are budgeted as they are embedded

Status: accepted. Post-M12; no milestone. Chunker 1.0.0 → 1.1.0. No schema change, no migration,
no change to any budget, threshold, validator rule or embedding contract.

## Context

"Head and Neck: Muscle Charts" stopped at `NEEDS_REVIEW` on `CHUNK_OVERSIZED`: a `TABLE_PART` of
**396** retrieval tokens against the **384**-token budget, on two attempts with chunker 1.0.0. The
validator was right — the encoder takes at most 512 tokens, 128 are reserved for the context field
and special tokens (`Settings.chunking_fits_the_encoder` enforces 384 + 128 ≤ 512 at startup), and
the embedding layer refuses rather than truncates. The defect was in construction.

Root cause, from the stored chunk and its cells (page 10, laryngeal muscles):

1. The innervation cell at row 2 has `row_span = 8`, covering rows 2–9. The parser recorded its text
   as the same value written once per covered row (eight copies), which inflates the group.
2. The table splitter closes row groups over row spans, so rows 2–9 formed one indivisible group.
3. The packer compared the budget only when appending to a non-empty part (`if current and …`). A
   group over the budget on its own was emitted whole: 396 > 384.
4. A naive row split would have been worse: a spanned value is printed only on its origin row, so
   rows 3–9 would have lost their innervation in any later part.

Auditing the other construction paths found the same class of gap: text children, lists,
term/definition pairs, question explanations and question preambles were budgeted on the **source**
text, but emitted as the **retrieval** representation, which prepends the hierarchy prefix
(`Context: … > …`). Only figures accounted for it.

## Decision

1. **Budget on the representation.** Every construction path measures what it will emit:
   `Builder.room(hierarchy, target)` subtracts the prefix's tokens (the prefix ends at whitespace,
   so tokens add), and table parts are measured through `representation()` directly. Measured with
   the pinned MedCPT tokenizer.
2. **Merged rows stay together when they fit.** Only a merged-row group that cannot fit on its own
   is split, at row boundaries, packing as many rows per part as fit.
3. **Carry merged cells, by name.** A part that holds rows covered by a merged cell printed on an
   earlier row starts with one line: `<Column header> (merged cell, also applies to these rows):
   <value>`. The value is not copied into each row as if it were separate cells; `carried_cells` in
   metadata records it.
4. **A single row too large for any part is divided by cells**, each fragment repeating the table
   context and the row's first cell, every other cell written as `Column: value`, over-long values
   cut at sentence then token boundaries and marked `(continued)`. If the context alone leaves under
   16 tokens of room, the row is emitted whole and validation stops it — shredding it would satisfy
   the budget and make every piece meaningless.
5. **A list item** too large for any chunk on its own is split at sentence boundaries rather than
   emitted whole; lists still split only between items otherwise, and the repeated list heading is
   budgeted for.
6. **A question keeps its explanation only if the joined chunk fits**, not merely the explanation.
7. **Chunker 1.1.0.** The version is part of the policy fingerprint, so no run built by 1.0.0 is
   reused as current.

Rejected: raising the budget (it is derived from the encoder contract and would only move the
cliff); truncating; accepting the finding; de-duplicating the repeated merged-cell text (that would
rewrite parsed source inside the chunker — normalization belongs to the parse layer).

## Consequences

- Rebuilt offline with 1.1.0, three healthy real documents — a 22-page atlas chapter, a scanned
  question bank and a 932-page textbook (4,278 chunks) — produce **byte-identical** chunk texts to
  their stored 1.0.0 runs. Head and Neck differs only in its two page-10 table parts: 366 and 223
  tokens, maximum 375, `PASS_WITH_WARNINGS`.
- Documents with hierarchy prefixes may get different, smaller child boundaries on rechunk.
- Validation is unchanged and remains the backstop: an oversized unit that reaches it still stops
  the dataset (`test_the_validator_still_refuses_an_oversized_unit_that_reaches_it`).
- Not solved here: atomic `QUESTION` and `FORMULA` chunks are not divided — splitting a question
  from its options or a formula is a semantic change — so an oversized one still stops for review.
- The parser's repetition of merged-cell text remains in the source; it is faithful to what was
  parsed and is carried as such.

Tests: `backend/tests/test_chunk_budget_units.py` (budget at several sizes with the production
tokenizer, header and provenance on every part, no cell lost, exact boundary and boundary − 1,
carried merged cells, single-row fragments, prefix-aware text children, gold corpus, validator
backstop, version bump), `test_m3_units.py::test_merged_rows_stay_together_when_they_fit`.
