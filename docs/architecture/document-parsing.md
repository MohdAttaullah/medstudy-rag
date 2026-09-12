# Document parsing (M2)

Implemented boundary: a `QUEUED` document version is parsed by Docling, normalized into a
parser-independent structure with page and coordinate provenance, validated by deterministic
quality rules, and left at `READY_FOR_CHUNKING`. No chunk, embedding, index or answer exists.
Versions remain database-constrained unsearchable throughout.

## Executable stage graph

```
QUEUED -> PARSING -> NORMALIZING -> ENRICHING -> READY_FOR_CHUNKING
                \          \             \
                 -> FAILED / QUARANTINED / NEEDS_REVIEW / CANCELLED
```

`READY_FOR_CHUNKING` is a new status added by the M2 migration; it is terminal for M2 and only
`CANCELLED` (or an explicit reparse) leaves it. `CHUNKING`, `EMBEDDING`, `INDEXING`,
`VERIFYING_INDEX` and `READY` exist in the enum for schema compatibility and are absent from both
the application transition table and the database guard, so no code path can enter them.

`backend/app/ingestion/state.py` remains the single owner of transitions; every edge writes job
status, version status, ordered stage history and an audit row in one transaction, and the
PostgreSQL trigger `m1_job_guard` rejects any illegal edge independently of the application.

## Parser abstraction

| Layer | Module |
|---|---|
| Parser-independent types and `DocumentParser` protocol | `backend/app/ingestion/parser/model.py` |
| Docling implementation (the only module importing Docling) | `backend/app/ingestion/parser/docling_adapter.py` |
| Error taxonomy and retryability | `backend/app/ingestion/parser/errors.py` |
| Deterministic normalization | `backend/app/ingestion/normalizer/text.py` |
| Quality rules | `backend/app/ingestion/validation/parse_quality.py` |
| Durable pipeline | `backend/app/services/parsing.py` |

The domain consumes `ParsedDocument`/`ParsedElement`/`ParsedTable`/`ParsedFigure`/`ParsedFormula`
only. Replacing the parser requires a new adapter and nothing else.

### Pinned parser

Docling **2.126.0** (`docling-core` 2.95.0, `docling-ibm-models` 4.0.2, `docling-parse` 7.17.0),
layout model `docling-project/docling-layout-heron` through the transformers engine, table
structure through TableFormer, formula enrichment through `CodeFormulaV2`, OCR through RapidOCR
3.9.2 on ONNX Runtime 1.29.0. The parser runs on CPU with a pinned thread count
(`accelerator_options`) so the same document is not parsed differently because a host has more
cores. Remote services and external plugins are disabled: parsing never calls out of the worker.

Every `ParseRun` records `parser_name`, `parser_provider`, `parser_version`,
`configuration_version` and a `configuration_fingerprint` (SHA-256 of the whole frozen policy),
so a parse can always be attributed to an exact parser build and policy.

**Known limit — a paragraph that spans a page break is anchored to its first page.** Docling
emits such a paragraph as one element carrying the page number where it *starts*, so the portion
physically printed on the following page is attributed to the preceding one. The text is not lost;
its page provenance is the paragraph's first page rather than the page each sentence appears on.

This was measured on the 932-page acceptance textbook. Page 517 carries nothing but the tail of a
paragraph that begins on page 516, so page 517 itself yielded only its two running headers and
parse validation raised `PAGE_CONTENT_LOST`. The paragraph's full 1378 characters are present in
the parse, on page 516. The behaviour is not caused by page windowing: page 517 yields exactly the
same two elements at a 3-page range, at the 25-page production window and at a 75-page range —
every context that includes page 516. Only converting page 517 entirely on its own differs, and
only because the page the paragraph belongs to is then absent.

The consequence is a citation that can point one page earlier than the sentence it supports, for
the spanning portion of such a paragraph. It is a provenance-precision limit, not content loss,
and it is independent of the conversion strategy.

**Known limit.** Layout classification is not bit-identical across CPU/BLAS environments. Cell
contents, grid geometry, page counts, text and reading order were stable between the Windows host
and the Linux container measured; *semantic labels* on borderline regions (page header/footer,
caption association, formula detection, list grouping) were not. The parse-quality thresholds and
the gold evaluation are therefore baselined per environment, and the test suite asserts our own
persistence and provenance rather than a specific model judgement.

## ParseRun versioning

`parse_runs` is the unit of reproducibility. A run records the source checksum and pinned object
version, the parser and policy identity, the raw artifact location, counters, timing, the
validation result and a safe error. Runs are never overwritten:

```
DocumentVersion
├── ParseRun 1   attempt 1, parsing-m2-v1, superseded (is_active = false)
└── ParseRun 2   attempt 2, parsing-m2-v2, active     (is_active = true)
```

A partial unique index (`uq_parse_runs_active_version`) plus a check constraint
(`NOT is_active OR status = 'SUCCEEDED'`) make "at most one active, successful parse dataset per
version" a database property, not a convention. Only the active run is eligible for M3.

**Idempotency.** Duplicate Celery delivery is fenced twice: the outbox message's `received_at` is
claimed under a row lock, and `_claim` may only move a job out of `QUEUED` under its own row lock.
If an active run already exists with the same parser version, policy fingerprint and source
checksum, the job advances without a second parse and the reuse is audited.

**Reparse** is explicit: `POST /api/v1/ingestion/jobs/{id}/reparse` (permission
`ingestion:reparse`) moves a `READY_FOR_CHUNKING`, `NEEDS_REVIEW` or `FAILED` job back through
`VALIDATING`, consuming one unit of the same bounded retry budget. Curator acceptance is the other
route out of `NEEDS_REVIEW` and consumes none, because it creates no run. The previous run stays active
until the new one succeeds, and an unchanged parser/policy/source is a no-op.

**Leases.** A run records `worker_identity`, `heartbeat_at` and `lease_expires_at`. The dispatcher
releases runs whose lease expired, failing the job with the retryable `PARSER_LEASE_EXPIRED`, so a
crashed worker leaves a retryable job rather than one stuck in `PARSING`.

## Artifacts in object storage

```
documents/<document-id>/<version-id>/
  original/source.pdf                      (M1, immutable)
  parsing/<parse-run-id>/
    docling.json                           complete structured parser output
    pages/page-0001.webp                   optional page previews
    figures/<figure-id>.png                extracted figure crops
```

The raw artifact is a JSON envelope: `artifact_schema`, the parser identity, the parser
configuration actually used, the source page count, and the complete Docling document export.
Binary page and picture rasters are removed from that JSON because they are stored separately as
objects; the JSON stays a faithful *structural* record instead of carrying base64 copies of the
same pixels. It exists for debugging, re-normalization without reparsing, parser comparison and
reproduction. PostgreSQL is never the only copy of the parse structure.

All artifacts of a run live under that run's UUID prefix and are immutable for it. Object storage
is private: artifacts stream only through authorized API endpoints, and no key, endpoint or
pre-signed URL is ever returned to a client.

## Normalized data model

| Table | Responsibility |
|---|---|
| parse_runs | One durable parse attempt: parser/policy identity, artifacts, counters, validation, lease, error |
| document_pages | 1-based page, size, rotation, rolled-up text, element count, source text-layer size, OCR indicator, preview |
| document_elements | Structural element: type, parent, sibling ordinal, reading order, raw and normalized text, bbox, parser reference |
| table_artifacts | Canonical cells, header rows, caption relation, continuation candidacy, markdown/HTML convenience renderings |
| figure_artifacts | Caption relation, stored crop, dimensions, media type, page and bbox |
| formula_artifacts | Source expression, whitespace-normalized expression, notation, adjacent explanatory paragraph |
| parse_validation_findings | Append-only severity/code/safe message/details, scoped to document, page or element |

### Page numbering

Page numbers are **1-based** everywhere they are persisted or displayed, matching the printed
order the parser reports. A check constraint (`page_number >= 1`) makes a 0-based index
unstorable. Grid coordinates inside a table (`row`, `column`) are 0-based and are never page
numbers; the two are separate fields and are never mixed.

### Bounding boxes

Stored in **PDF points** with a **TOPLEFT** origin: `(bbox_x1, bbox_y1)` is the upper-left corner
and `(bbox_x2, bbox_y2)` the lower-right, always paired with the owning page's `width`/`height`.
Docling reports a BOTTOMLEFT origin; the conversion happens once, in the adapter, against the page
height. Rotation is already applied by the parser backend, so a box is expressed against the
upright page and `rotation` is recorded for reference. `bbox_origin` is stored alongside every
box, so a future citation viewer never has to guess the convention. An element the parser did not
locate has all five columns NULL — explicitly unlocated rather than defaulted to zero.

### Hierarchy and reading order

`parent_element_id` is set only from the parser's own containment (`self_ref`/`parent`), never
reconstructed from geometry. `reading_order` is the document-wide depth-first traversal position
and is unique per run; `ordinal` is the position among siblings. `depth` records nesting level.
Structural containers (a list, a chapter) are persisted as elements with no page and no box; that
absence is expected and is excluded from the unlocated-element rule.

### Tables

`cells` is the canonical representation: text plus 0-based row/column, spans and header flags, so
a future chunker can split a large table while keeping header semantics. `markdown` and `html` are
convenience renderings, never the source of truth. Continuation across pages is *flagged*, never
merged: a later table is marked `possible_continuation` only when it is on the immediately
following page, has the same column count, declares no header row while the previous table does,
and the evidence string records why. Uncertain pairs stay separate tables.

### Figures and formulas

A figure keeps its page, box, media type, dimensions and the stored crop. A formula keeps the
source expression exactly as parsed and a whitespace-only normalization; symbols, operators and
spacing inside tokens are untouched, and no model is asked to reinterpret, complete or correct an
expression. The related-text link points only at an adjacent paragraph — a caption belongs to its
own figure or table, and a heading is not an explanation.

### Captions

Figure↔caption and table↔caption relations come from the parser's declared relation only. When
the parser reports no relation, `caption_element_id` and `caption_text` are both NULL and the UI
says so. Geometric proximity is never used to invent a relation.

### Headers, footers and footnotes

Repeated running heads and feet are classified (`PAGE_HEADER`, `PAGE_FOOTER`) and retained; they
are never deleted, because a footer can carry a qualification that changes the meaning of the page
above it. Footnotes are preserved with their page and, where the parser exposes it, their parent.

### Question-bank cues

Numbered stems, option blocks, `Answer:` and `Explanation:` survive as ordinary elements with
their reading order. M2 derives no question object, no key and no answer; `structure_inferred`
stays false because nothing here is inferred. That logic belongs to M3.

## Normalization policy

Allowed and applied: Unicode NFKC, soft-hyphen removal, ligature expansion, zero-width and BOM
removal, hyphenated line-break repair, single-newline joining, horizontal whitespace collapsing,
blank-line capping and per-line trimming.

Never applied: any change to numbers, units, doses, drug names, abbreviations, terminology or
formulas. Nothing is "corrected" because it looks clinically implausible — a genuine source error
must stay visible. When normalization changes the text at all, `raw_text` keeps the parser's
original and `text_normalized` records that the two differ, so every transformation is auditable.

## OCR policy

`ocr_mode` is `OFF`, `AUTO` (default) or `FORCE`. `AUTO` lets Docling OCR only regions with no
text layer, so a text PDF is never rasterized needlessly and a scan is still recovered. `FORCE`
OCRs whole pages. The engine (RapidOCR) is recorded on the run.

Per page, `source_text_chars` records what the PDF text layer alone yields, measured with pypdf
before any OCR. A page with essentially no text layer whose parse nonetheless produced text is
recorded as `ocr_used` with `ocr_evidence = no_source_text_layer`. This is a derived indicator,
documented as such, not a parser claim. A page that is OCR-derived *and* nearly empty is reported
as suspicious rather than accepted; widespread suspicious OCR routes the whole document to review.
No frontier or vision model is used as OCR, and no model is used to interpret a figure in M2.

## Quality validation

A successful Docling run is not a valid parse. After normalization the deterministic rules in
`parse_quality.py` produce persisted findings (`INFO`/`WARNING`/`ERROR`/`CRITICAL`) and one
structured result:

| Result | Meaning | Job outcome |
|---|---|---|
| PASS | No findings at all | READY_FOR_CHUNKING |
| PASS_WITH_WARNINGS | Only INFO/WARNING findings | READY_FOR_CHUNKING |
| NEEDS_REVIEW | Any ERROR, or a page-count mismatch | NEEDS_REVIEW |
| FAIL | Any CRITICAL finding | FAILED |

Document rules: page-count reconciliation against the source container, non-empty page ratio,
element density, invalid-bbox ratio, unlocated-element ratio, malformed-table ratio, suspicious-
OCR page ratio, empty-formula ratio, reading-order contiguity and page-regression count, plus a
parser partial-success warning. Page rules: invalid dimensions, empty page, content lost (a page
with a text layer that produced nothing), suspicious OCR, missing optional preview. Artifact
rules: empty table, cells outside the declared grid, missing figure artifact, empty formula,
unresolved caption reference.

Thresholds are typed and frozen (`ParseThresholds`), snapshotted per run and included in the
policy fingerprint, so a silently edited threshold cannot reuse a parse produced under different
rules. No rule reports an "accuracy percentage"; findings carry codes, counts and page numbers,
never document text.

## Bounded memory: page-window conversion

A whole-book conversion is not bounded-memory. Docling retains per-page state for the life of one
`convert()` call, so peak RSS grows linearly with page count: measured on this host, a synthetic
document cost ~18 MiB per page, and a **real 50-page textbook section cost 2442 MiB — 44 MiB per
page**, because real pages carry tables, figures and page images. A 932-page medical textbook on
that path reached 6.2 GB and the Celery child was `SIGKILL`ed by the kernel OOM killer
(`oom_kill 1`, container itself not `OOMKilled`), which surfaced only as `WorkerLostError`.

Long documents are therefore converted in **page windows**. Each window is a separate Docling
call over `page_range=(first, last)`; the window's state is released before the next one is
allocated, and measured peak RSS then **plateaus regardless of book length** — 200 pages 1389 MiB,
400 pages 1571 MiB on synthetic content.

| Setting | Default | Meaning |
|---|---|---|
| `MEDRAG_PARSING__PAGE_WINDOW_SIZE` | 25 | pages per conversion call; `0` disables windowing |
| `MEDRAG_PARSING__PAGE_WINDOW_THRESHOLD` | 25 | documents at or below this convert in one call |

The threshold equals the window size, so **no conversion call ever handles more pages than one
window** and the memory bound is uniform. Every fixture in this repository is three pages or
fewer, so fixtures keep the single-call path and existing parse output — including the
determinism the M2 evaluation asserts over the fixture corpus — is unchanged. Windowing takes
over only where the single-call path is the thing that fails.

### What else grows with book length

Windowing bounds Docling's own per-page state. Two other costs scale with the document and had to
be bounded before a 932-page book could actually finish.

**The source text layer.** Every page's parser output is compared against the characters the PDF's
own text layer offers, which is how a scanned page is distinguished from a parsed-empty one. Read
for the whole book at once, pypdf caches every page's decompressed content stream as it goes: on
this textbook that measured ~1.3 GiB, paid before Docling had converted a single page. It is now
read one window at a time, so it costs the same bounded amount whatever the length of the book.

**Freed memory the allocator keeps.** With both of the above in place, worker RSS still ratcheted
~6 MiB per page across the book while the parse output actually retained grew ~0.2 MiB per page
(42 MiB of previews, figure crops and raw artifact per 200 pages). The objects were being freed;
glibc was keeping the arenas. The parser therefore trims the heap between windows, and the worker
caps `MALLOC_ARENA_MAX`. Measured over the same eight windows of real textbook content, settled
RSS went from *climbing* 2267 → 2842 MiB to *flat* 1248 → 1321 MiB.

The remaining peak is the working set of one window, not of the book: it is bounded by
`page_window_size`, so a deployment that needs a lower ceiling lowers the window size.

### Provenance across windows

**Absolute page numbers are preserved.** Docling reports absolute page numbers under
`page_range`, so page 101 parsed in the window 101–125 remains page 101; no page number is ever
translated from a window-local index, and pages are joined by page number with duplicates
dropped.

What does need care is Docling's **reference space**, which restarts at `#/texts/0` in every
window. Joined naively, the second window's `#/texts/0` would be the same key as the first's and
a parent lookup would silently re-parent an element into a different part of the book. Every
reference is therefore namespaced by window (`w3#/texts/4`), and `parent`, `caption_of` and
caption references take the same prefix, so every relationship stays inside the window where the
parser actually established it. `reading_order` is assigned by the join and is continuous across
the whole document, not carried from each window's own counter. Nothing else about an element —
text, type, bounding box, ordinal, depth, page — is altered by joining.

The **raw artifact** keeps each window's Docling export whole, labelled with its page range, under
an envelope carrying the window plan. A fabricated single export would claim a document Docling
never produced, and the point of that artifact is to record exactly what the parser emitted.

### Time budgets

Windowing makes a long parse succeed, which makes it *long*: the 932-page book is hours of work,
not minutes, so a constant timeout cannot serve both it and a one-page leaflet.

| Bound | Setting | Default | Scope |
|---|---|---|---|
| One conversion call | `MEDRAG_PARSING__TIMEOUT_SECONDS` | 900 s | one window, or a short whole document |
| Whole document | `document_timeout_for(pages)` | 900 s + 12 s/page, capped 14400 s | every window together |
| Celery soft / hard | `MEDRAG_PARSING__TASK_SOFT_TIMEOUT_SECONDS` / `__TASK_TIMEOUT_SECONDS` | 15600 s / 15900 s | the whole task |

The whole-document budget is checked **between** windows — a conversion call is opaque, so the
seams are the only place it can be enforced — which means a parse already over budget still
finishes the window it is in. The soft task limit must therefore clear the budget *plus* one
conversion call, or the worker would kill the task before the parser could report a diagnosable
`PARSER_TIMEOUT`. A validator enforces the whole chain rather than leaving it to defaults.

### The lease during a long parse

The parse lease is what makes a lost worker recoverable: `reap_expired_leases` moves an expired
run to `FAILED` with `PARSER_LEASE_EXPIRED` and the job becomes retryable. Before windowing the
lease was renewed only at stage boundaries, and a book took minutes; now a book takes hours, and a
lease renewed only at stage boundaries would be reaped out from under a **healthy** parse.

The parser therefore reports progress between windows (`on_progress(pages_done, pages_total)`) and
`ParseService` renews the lease on each beat, logging `PARSE_WINDOW_COMPLETED`. The lease is sized
for **liveness, not for document length**: it must outlive one conversion call, because nothing
can renew it from inside one, and a validator refuses a lease shorter than that. It does not have
to outlive the document — which is what keeps the detection window for a dead worker bounded no
matter how long the book is.

## Reviewed acceptance of a flagged parse

`NEEDS_REVIEW` is a real verdict, not a soft warning, and it is never rewritten. An authorized
curator can nonetheless admit the parse for further processing, and that judgement is recorded as
a separate immutable fact.

```
ParseRun  status = FAILED, validation_result = NEEDS_REVIEW      flagged by the quality layer
    +  POST .../parse-runs/{id}/review  {decision: ACCEPT, rationale}
ParseRun  status = REVIEWED_ACCEPTED, validation_result = NEEDS_REVIEW, is_active = true
IngestionJob  NEEDS_REVIEW -> READY_FOR_CHUNKING        no retry consumed, no new parse run
```

The parse run keeps `validation_result = NEEDS_REVIEW` permanently and keeps every
`parse_validation_findings` row; `parse_review_decisions` records the reviewer, the rationale, the
correlation id, the exact `configuration_fingerprint`, and a `findings_digest` over the finding set
that was reviewed. The decision table is append-only at the database, like findings and audit
events, and its foreign key binds it to one exact run — so acceptance cannot follow a reparse,
which produces a new run with no decision of its own.

| Concern | Where it is enforced |
|---|---|
| Only a flagged parse can be accepted | `parse_review_guard`: `REVIEWED_ACCEPTED` requires `FAILED` + `NEEDS_REVIEW` |
| An accepted run really has a decision | `parse_review_guard`: an ACCEPT row for that run must exist |
| The automated verdict is immutable | `parse_review_guard`: `validation_result` cannot change once written |
| One decision per run | partial unique index `uq_parse_review_decisions_accept` |
| Acceptance costs no retry | `m1_job_guard` admits the edge only with `retry_count` unchanged |
| The edge needs a real acceptance | `m1_job_guard` requires an active `REVIEWED_ACCEPTED` run for the version |
| A reviewed parse can be chunked | `m3_run_guard`, both clauses, plus `usable_parse` in Python |

**Chunking eligibility** is therefore two clauses, not one: the unchanged automatic path
(`SUCCEEDED` with `PASS`/`PASS_WITH_WARNINGS`), or `REVIEWED_ACCEPTED`. Because a reviewed parse
keeps `NEEDS_REVIEW` forever, anything that gates on "is this parse usable" must consult the run
**status**, never `validation_result` alone.

Permission `ingestion:accept`, held by curator and admin, never reader. Acceptance is audited as
`PARSE_REVIEW_ACCEPTED`; the rationale lives only in the decision row, because it is operator prose
that may quote the document and audit metadata must stay safe to log.

## Failure and retry semantics

Fail-closed. A run is complete only when the raw artifact exists, pages, elements and artifacts
are persisted, and validation has run. `is_active` is set in the same transaction that reaches
`READY_FOR_CHUNKING`, so a crash at any earlier point leaves diagnostic rows that no consumer can
mistake for valid parse data.

| Code | Retryable | Typical cause |
|---|---|---|
| PARSER_SOURCE_MISSING, PARSER_SOURCE_CORRUPT, PARSER_UNSUPPORTED_PDF, PARSER_PAGE_LIMIT_EXCEEDED | no | deterministic input defect |
| NORMALIZATION_FAILED, NORMALIZATION_INVALID_STRUCTURE, PARSE_VALIDATION_FAILED, PARSE_NEEDS_REVIEW, PAGE_PREVIEW_STORAGE_FAILED | no | deterministic output defect |
| PARSER_TIMEOUT, PARSER_OOM, PARSER_UNAVAILABLE, PARSER_LEASE_EXPIRED, PARSER_INTERNAL_ERROR, OCR_FAILED | yes | environment |
| RAW_ARTIFACT_STORAGE_FAILED, FIGURE_ARTIFACT_STORAGE_FAILED, PARSE_PERSISTENCE_FAILED | yes | infrastructure |

An unknown code is treated as non-retryable so an unclassified defect stays visible. Messages are
fixed operator-safe strings; vendor exception text, paths and model details never reach durable
state or an API response. Cancellation and archival are honoured between stages: a job that left
the parse path releases its run as `CANCELLED` with no normalized rows.

## Worker pipeline

```
transactional outbox -> Celery receipt (claims the message under lock)
  -> _claim: QUEUED -> PARSING, ParseRun created RUNNING with a lease
  -> download original to a per-job temporary directory
  -> Docling conversion (one call, or page windows joined by absolute page number,
     renewing the lease between windows)
  -> raw artifact written to object storage and pinned on the run
  -> NORMALIZING: pages, elements, tables, figures, formulas in one transaction
  -> ENRICHING: page text rollups, counters, table continuation candidates
  -> validation -> READY_FOR_CHUNKING | NEEDS_REVIEW | FAILED
```

Enrichment is deterministic. No OpenAI, Anthropic or other provider is called anywhere in the
parse path. Per-job temporary directories are removed on success, failure and cancellation alike.
The correlation ID follows the job from upload through every parse stage event and audit row.

## Configuration

`MEDRAG_PARSING__...` supplies the frozen `ParsingConfig`. See
[configuration](configuration.md#m2-parsing-settings) for the field table.

## Deployment

Only the Celery worker carries the parser: `infrastructure/docker/worker.Dockerfile` installs the
`parsing` extra and the X/GL runtime libraries the OCR engine's OpenCV wheel links against. The
API and dispatcher stay on the lean backend image. Torch resolves from the PyTorch CPU index on
Linux (`[tool.uv.sources]` in `pyproject.toml`), which keeps the worker image at ~2.5 GB instead
of pulling several gigabytes of CUDA runtime that this CPU deployment cannot use. Model weights
download on first use into the `parser-models` named volume mounted at `/home/medrag/.cache`.

See [ADR-001](../adr/ADR-001-document-parser.md) and
[ADR-007](../adr/007-m2-parse-runs-and-quality-validation.md).
