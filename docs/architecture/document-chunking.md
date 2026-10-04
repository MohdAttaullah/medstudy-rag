# Document chunking (M3)

Chunking turns one validated parse run into a durable, versioned dataset of retrieval units with
complete provenance. It runs after [document parsing](document-parsing.md) and stops at
`READY_FOR_EMBEDDING`. **A chunked version is not searchable.** No embedding, vector, index or
answer exists at this milestone, and every version remains database-constrained unsearchable.

## Input contract

The only input is the **active** `ParseRun` for the version (`is_active`, `SUCCEEDED`, validation
`PASS` or `PASS_WITH_WARNINGS`) and its persisted rows: pages, elements, tables, figures, formulas.
The raw Docling artifact in object storage is a debugging and re-normalization record, never a
chunking input. `backend/app/repositories/chunking.py` loads those rows into the frozen, ORM-free
contracts in `backend/app/ingestion/chunking/model.py`, so the builder cannot reach the database,
the parser library or object storage.

If the active parse run changes, disappears or stops validating while a chunk run is in flight, the
run is fenced and fails rather than completing against a source that no longer exists.

## What determines a chunk boundary

M2 measured that layout **classification** is not bit-identical across CPU/BLAS environments: the
same bytes and the same parser version agreed on page counts, page text, table cells, grid geometry
and reading order, but disagreed on borderline semantic labels. Chunking therefore derives structure
only from signals that proved stable:

| Used | Not used as a boundary signal |
|---|---|
| page number and page geometry | caption vs paragraph classification |
| document-wide reading order | page-header / page-footer classification |
| normalized source text and its offsets | formula semantic labelling |
| parser-declared parent links (ancestry) | list-item grouping by label |
| explicit artifact relations (table/figure/formula → element, caption, footnote, neighbour) | |
| bounding boxes, for repeated-margin detection only | |

`test_borderline_labels_do_not_change_chunk_hashes` relabels every element in a fixture and asserts
the chunk identities are unchanged. The claim this supports is precise:

* **Guaranteed.** Identical normalized input + identical policy + identical tokenizer produce
  identical boundaries, source mappings, retrieval text, hierarchy and chunk hashes.
* **Not guaranteed, and not claimed.** That the same PDF parsed on Windows and on the Linux worker
  produces the same normalized input in the first place. That is an upstream M2 property; see
  [document parsing](document-parsing.md).

## Structure

Regions are cut at declared ancestry changes and at deterministic numbered headings. Within a
region the builder forms units in this order of preference: section/subsection boundary, paragraph
boundary, list boundary, sentence boundary, and token slicing only as a last resort. Token slicing
uses tokenizer **offsets into the normalized source**; decoded token text is never used to
reconstruct source, so capitalization, symbols and units survive intact.

* **Parent chunks** (`TEXT_PARENT`) are coherent structural context, default target 1280 tokens.
* **Child chunks** (`TEXT_CHILD`, `LIST`) are the precise retrieval units, default target 384.

Both targets are configuration, not constants. Lists stay with their heading and split only between
whole items; an explicit `term:` label keeps its following definition.

## Specialized handling

**Tables** never go through the text splitter. Canonical cells remain the source of truth. A small
table is one `TABLE` chunk; a large one splits into `TABLE_PART` chunks on row-span-aware row
groups, and every part repeats the identical header rows and keeps its caption, its row indexes and
its selected cells in metadata. Footnotes linked to the table element are attached. A parser-flagged
continuation is recorded, never destructively merged.

Rows joined by a merged cell stay in one part **when they fit**. A merged-row group that cannot fit
even on its own (chunker 1.1.0, ADR-027) is split at row boundaries instead, and each later part
carries the merged cell once, at the top, named by its column header and marked as a merged cell —
`Innervation (merged cell, also applies to these rows): …` — so no row loses a value that the
source printed only on an earlier row, and the value is never copied into each row as if it were
separate cells. Such parts record the carried cells in `carried_cells`. A single row too large for
any part is divided by cells: each fragment repeats the table context and the row's first cell, and
writes the rest as `Column: value`, cutting an over-long value at sentence and then token boundaries
and marking the pieces as continued (`row_fragment` in metadata). Nothing is truncated or dropped.
If the table context alone leaves too little room for that to mean anything, the row is emitted
whole and validation stops it rather than shredding it into meaningless pieces.

**Formulas** are atomic `FORMULA` chunks carrying the expression exactly as parsed plus only the
neighbouring elements M2 explicitly linked. No model interprets, completes or corrects a formula.

**Figures** produce `FIGURE_CONTEXT` chunks from the source caption, deterministically linked
neighbouring text, hierarchy and the `FigureArtifact` id. There is no visual reasoning in M3: the
stored crop remains the visual source, and a figure with no source text is labelled as such rather
than described.

A figure legend long enough to exceed the retrieval budget is **split**, as a large table is, into
several `FIGURE_CONTEXT` chunks carrying `part_number` and `part_count`; an ordinary figure remains
one chunk with no part metadata. The legend is split rather than shortened because the caption
element is consumed by the figure chunk: text left outside a bounded representation would belong to
no retrieval unit at all. Every part keeps the figure element, the artifact id, the complete caption
in metadata and exact source offsets, and boundaries fall on sentence boundaries where they exist
and on tokenizer boundaries otherwise, so nothing is invented and nothing is dropped.

**Question banks** produce `QuestionArtifact` objects with options, page provenance and the
document's authority metadata. Question and options are one atomic `QUESTION` chunk. An explanation
stays with the question when it fits and otherwise becomes a linked `QUESTION_EXPLANATION` child.
`Answer: B` is stored as `B`; **when the source states no answer, `explicit_answer` is NULL and
nothing is inferred** — a database check constraint (`never_infer_answers`) makes an inferred answer
unstorable. Grouping that the source did not state explicitly is marked `structure_inferred`, and a
question whose option ordering or stem is ambiguous is routed to review rather than guessed.

Repeated page margins are detected by bounding box and repetition across pages, and are *excluded
from chunking* while remaining fully present in the parse run. Nothing is deleted.

## Source text and retrieval text

Each chunk stores both. `normalized_text` is faithful source. `retrieval_text` may deterministically
prepend declared hierarchy context (`Context: Chapter > Section`) and, for tables, the caption and
repeated headers. There are no model-generated summaries and no invented content; the UI labels the
added context as not being source evidence.

`retrieval_text` is what the encoder receives as its input body, so it — not `normalized_text` — is
what the retrieval budget bounds and what chunk validation measures when deciding whether a chunk
could be embedded at all. Since chunker 1.1.0 every construction path budgets on it too: text
children, lists, term/definition pairs, question explanations, preambles, figures and table parts
all leave room for the hierarchy prefix they will be emitted with, measured with the pinned
tokenizer. Validation remains the fail-closed backstop. The source text is only a lower bound on it: the hierarchy prefix, and a
figure's no-text placeholder, are embedded but are part of no source text.

## Identity and reuse

`chunk_hash` is a SHA-256 over version, parse run, policy fingerprint, chunk kind, source text,
retrieval text, source spans, artifact ids, hierarchy and metadata. The row's UUID is a database
key only. `ChunkRun.input_fingerprint` is a SHA-256 of the whole normalized input.

A completed run is reused for a new request only when the parse run, the policy fingerprint (which
covers chunker version, targets, thresholds and the pinned tokenizer) **and** a freshly recomputed
input fingerprint all match. Anything that cannot be proved identical is rebuilt. A forced rechunk
always builds a new run; for identical input it produces new row ids with identical chunk hashes.

## Tokenizer

The MedCPT Article Encoder WordPiece tokenizer is bundled under
`backend/app/ingestion/chunking/assets/medcpt/`, with its revision, file SHA-256 and the
`tokenizers` runtime version pinned inside the policy. It is used **only to count and slice tokens**.
No weights are loaded, nothing is downloaded at runtime and nothing is embedded. A missing, altered
or version-mismatched tokenizer fails closed with `CHUNK_TOKENIZER_LOAD_FAILED`.

## Quality validation

`backend/app/ingestion/chunking/quality.py` produces persisted findings and one result:
`PASS`, `PASS_WITH_WARNINGS`, `NEEDS_REVIEW` or `FAIL`. Only `PASS`/`PASS_WITH_WARNINGS` can become
the active dataset. Checks include empty and oversized chunks, tiny orphans, token-count mismatch
against the pinned tokenizer, missing or invalid provenance, invalid page ranges, broken or missing
parents, duplicate content identity, question/option splits, table header loss, empty formulas,
missing figure relationships, invalid hierarchy and hash collisions.

Source coverage is measured by **text**, not by identity. For every eligible element the union of
its mapped span intervals must cover all of its non-whitespace characters:

* a fragment no chunk covers → `CHUNK_SOURCE_TEXT_OMITTED` (CRITICAL);
* a fragment only a parent still carries, dropped by the child split →
  `CHUNK_SPLIT_TEXT_OMITTED` (ERROR);
* whitespace between spans is not loss.

Metrics record chunk counts by type, parent/child token distributions, eligible vs mapped elements,
missing provenance, omitted characters, question option splits, table header loss, tiny and
oversized counts and a dataset hash. **None of these is a retrieval or medical accuracy measure**,
and no percentage of correctness is claimed anywhere.

## Durability

`READY_FOR_CHUNKING → CHUNKING → VALIDATING_CHUNKS → READY_FOR_EMBEDDING`, in both the application
transition table and the database trigger. Work is dispatched through the existing transactional
outbox with a distinct `CHUNKING` message kind, so duplicate delivery is idempotent: the receipt is
claimed once, and a run already claimed is not rebuilt. A fenced lease with a token, a timeout and
dispatcher sweeping means a crashed worker becomes a retryable failure rather than a stuck job.
Chunks, mappings, questions, relations, findings and active-run selection commit in one transaction
after validation; a failure leaves no partial dataset and no active run.

Historical completed runs stay inspectable and are immutable: database triggers refuse changes to a
completed run's identity and refuse any insert, update or delete on the rows of a run that is not
`RUNNING`. At most one active dataset exists per version, and superseding a parse run or cancelling
a job deactivates its chunk datasets automatically.

Deterministic input defects (`CHUNK_TABLE_FAILED`, `CHUNK_VALIDATION_FAILED`,
`CHUNK_TOKENIZER_LOAD_FAILED`, …) are never retried; transient ones
(`CHUNK_PERSISTENCE_FAILED`, `CHUNK_TIMEOUT`, `CHUNK_LEASE_EXPIRED`) are. Phase events
(`CHUNK_TABLES_STARTED`, `CHUNK_QUESTIONS_STARTED`, `CHUNK_TEXT_STARTED`,
`CHUNK_VALIDATION_STARTED`, …) are emitted from the point the corresponding work actually begins,
and a phase with no source material is never announced.

## Inspection

Tenant-authorized read APIs expose chunk runs, chunks (filterable by type, page, parent, question,
table, figure, formula and validation state — `status=blocking|warning|clean|findings`), chunk
detail, ordered source mappings, question artifacts, validation findings and a review summary
(`/chunk-runs/{id}/review-summary`: chunk, question and finding counts, blocking vs warning, and how
many chunks are in each state). Each listed chunk carries its own findings, worst first, so its
state is visible without a second request; a chunk is in the state of its worst finding.
`CHUNK_OVERSIZED` findings record the measured and allowed token counts. No endpoint returns a storage key, a lease token or any vector field. The
Chunk Inspector shows the source text, the retrieval representation, tokens, hash, hierarchy,
parent/child relations, artifact relationships, the structured table view and a link back to the
exact source page in the Parse Inspector. A stopped dataset opens on its findings; the views carry
counts, and the findings view is flagged with its blocking count when another view is open. Each
chunk shows its state in words (Blocking, Warning, No findings) with the measurement for size
findings (`396 / 384 retrieval tokens`). "Inspect this issue" deep-links to the affected chunk with
the list filtered to blocking chunks, and a blocking chunk shows a short decision guide and the
reprocessing actions the server allows (see `document-lifecycle.md`).

## Deliberately not in M3

Embedding generation, vector representations, Qdrant indexing, BM25, dense or hybrid retrieval,
fusion, reranking, provider calls and medical answering. The embedding and index job states remain
unreachable in both the application graph and the database guard.
