# ADR-025: A page's text is lost only when it is not in the parse

Status: accepted. Post-M12 acceptance hardening; no milestone was created. Corrects the page-level
validator from ADR-007. No parser change, no schema change, no weakening of any existing ERROR.

## Context

`parse_quality` decided page content loss from two character counts:

```python
if page.text_chars < min_chars_per_page:
    severity = ERROR if page.source_text_chars >= min_chars_per_page else WARNING
```

That rule cannot distinguish two situations which look identical from page N — few parsed
characters, many source characters:

- **A.** the parser dropped the text, and it is gone;
- **B.** the parser kept the text and anchored it to the page the paragraph *began* on.

B is correct behaviour. Docling anchors a cross-page paragraph to its first page, which leaves the
continuation page looking empty while nothing at all is missing.

On the real 932-page Medical Microbiology textbook the rule produced exactly two ERRORs, and
**both were false**:

| Page | parsed | source | What it actually is |
|---|---|---|---|
| 44 | 37 | 56 | A section divider. Its decorative `S E C T I O N` lettering does not survive tokenization; its title *"BASIC CONCEPTS IN THE IMMUNE RESPONSE"* was parsed. |
| 517 | 28 | 815 | The tail of a paragraph beginning on 516. Page 516 parsed **6891** characters against its own source layer of **6282** — the surplus is 517's text. |

Two false ERRORs forced a 932-page document into `NEEDS_REVIEW` and required a curator decision
(ADR-018) to proceed. The validator was reporting a parser behaviour as a parser failure.

## Decision — ask whether the text is in the parse

Character counts stay as the *first* stage: a page below the floor whose source layer is above it
is **suspect**. What changes is that a suspect page is then checked directly —

> Is this page's source text present in the parsed document?

`text_recovery.coverage` compares **material words**: tokens of two characters or more, casefolded,
as a set. Coverage is computed twice, against the page's own parsed text and against its immediate
neighbours, giving three outcomes:

| Condition | Finding | Severity |
|---|---|---|
| own coverage ≥ `min_page_text_recovery` | `PAGE_TEXT_BELOW_FLOOR` | WARNING |
| neighbourhood coverage ≥ threshold | `PAGE_CONTENT_ANCHORED_ELSEWHERE` | WARNING |
| otherwise | `PAGE_CONTENT_LOST` | **ERROR** |
| not measured at all | `PAGE_CONTENT_LOST` | **ERROR** |

The two warnings are distinct because they mean different things: one says the shortfall was page
furniture, the other says the text is on a neighbouring page and names which. Findings carry
`parsed_chars`, `source_text_chars`, both coverages, the threshold and `compared_pages`, so a
reviewer can go and look.

### Why words, and not characters or n-grams

A character ratio cannot express "present but elsewhere" at all — it only measures how much of a
page is on that page, which is the question that produced the false ERRORs.

Word 5-grams were implemented and measured first. They scored page 517 at 1.000 but page 44 at
**0.200**, because letter-spaced display text shingles into fragments like `c t i o n`. That is a
property of the text extractor, not of the document.

Set-of-material-words is insensitive to re-flowed whitespace, to reading-order differences between
the text layer and the parser, and to the duplication a cross-page join produces. Measured on the
real book it scores **1.000 for both** suspect pages, against 0.80–0.99 for ordinary pages — so the
threshold is "essentially all of it", not a tuned midpoint between two clusters.

### Bounded cost

Source text is re-extracted **only for suspect pages** — two of 932 on the real book. Extracting a
whole book at once is not bounded memory: pypdf retains each page's decompressed content stream,
measured at ~1.3 GiB on this document, and this runs after conversion when that headroom is least
available. The file is still in the parse workspace, so no re-download and no reparse occurs.

## What is deliberately unchanged

- **No ERROR is downgraded silently.** A page whose text is genuinely absent is still
  `PAGE_CONTENT_LOST` / ERROR, and partial recovery below the bar is still loss.
- **Unmeasured fails closed.** No source file, an unreadable PDF, or a missing measurement leaves
  the page an ERROR. Absence of evidence is not evidence of preservation.
- A page with no source text at all is still `PAGE_EMPTY` / WARNING, as before.
- Every other rule — dimensions, OCR suspicion, bbox, reading order, page ratios — is untouched.
- The reviewed-acceptance workflow (ADR-018) is unaffected: a document that still raises a genuine
  ERROR still goes to `NEEDS_REVIEW` and still requires a curator decision.

## Consequences

Re-running the new validator over the **stored** parse of the real book, read-only:

```
page 44:  own=1.000 nbhd=1.000 pages=(43,44,45)    -> PAGE_TEXT_BELOW_FLOOR (WARNING)
page 517: own=0.046 nbhd=1.000 pages=(516,517,518) -> PAGE_CONTENT_ANCHORED_ELSEWHERE (WARNING)
page-loss ERRORs: 0
```

A document like this one would now parse to `SUCCEEDED` rather than `NEEDS_REVIEW`, without any
material text having been accepted as lost — because none was lost.

## Limitations

The regression evidence is the **stored** parse plus the original PDF, replayed read-only. The
change was not exercised by a fresh ingestion of the 932-page book: that costs ~67 minutes, consumes
a parse retry, and would disturb the corpus the acceptance tests depend on. The computation
validated here is the same function the validator now calls, on the same inputs it would receive,
but the end-to-end ingestion path for this specific document has not been re-run.

`NEIGHBOURHOOD` is one page either side. That covers paragraph-start anchoring, which is the
behaviour this exists for. A document whose parser scattered text further than one page would
still be reported as loss — deliberately, since widening the window would start excusing real
loss.
