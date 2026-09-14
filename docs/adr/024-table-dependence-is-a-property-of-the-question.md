# ADR-024: Table dependence is a property of the question, and `cell` is not a table word

Status: accepted. Post-M12 acceptance hardening; no milestone was created. Applies ADR-023's
reasoning to tables and narrows ADR-012's table rule to what ADR-012 says. M8 is unchanged.

## Context

ADR-012 states it the same way it stated the figure rule:

> *"A table **question** whose table part arrives without its header rows is `INSUFFICIENT`."*

The implementation was wider in two independent ways.

### 1. `cell` was a table cue

`TABLE_CUES` contained `cell`. In a medical corpus that is ordinary subject vocabulary. Measured
against `sparse_terms` over 3,939 indexed chunks of the real Medical Microbiology corpus:

| Cue | documents | % of chunks |
|---|---|---|
| **`cell`** | **920** | **23.36%** |
| `table` | 672 | 17.06% |
| `rows` | 3 | 0.08% |
| `column`, `columns` | 2 | 0.05% |
| `row`, `grid` | 1 | 0.03% |
| `tabulated` | 0 | 0.00% |

`cell` is three orders of magnitude away from every other cue. Cell wall, cell membrane, T cell,
cell-mediated immunity, host cell — this is what a microbiology question is *about*.

The consequence, measured on six ordinary questions (none of them from the gold set, so none of
this was tuned to it):

```
TABLE_DEPENDENT  SUFFICIENT    tables=1  Describe prokaryotic cell structure.
TABLE_DEPENDENT  INSUFFICIENT  tables=0  What is the function of the bacterial cell wall?
TABLE_DEPENDENT  INSUFFICIENT  tables=0  How does a T cell recognise antigen?
TABLE_DEPENDENT  INSUFFICIENT  tables=0  What is cell-mediated immunity?
TABLE_DEPENDENT  INSUFFICIENT  tables=0  What happens to the host cell during lytic viral replication?
TABLE_DEPENDENT  INSUFFICIENT  tables=0  Which organisms lack a cell wall?
```

Five of six refused with `TABLE_STRUCTURE_INCOMPLETE` on evidence that contained no table at all.
Beyond the wrong outcome, the reason code actively misleads: a curator reading it for *"What is
cell-mediated immunity?"* would go looking for a broken table.

The singular/plural asymmetry made this partly invisible — `cell` was a cue but `cells` was not, so
*"Which cells produce defensins?"* escaped by accident.

### 2. The structural fallback

```python
if kinds & TABLE_CHUNKS:
    return "TABLE_DEPENDENT"
```

The same `any anchor` shape ADR-023 removed for figures. Of the 8 real `TABLE_DEPENDENT`
classifications on the gold set, **7 came from a ranked table rather than from the question**, and
only 1 from a cue. It was mostly harmless only because the ranked table usually declared header
rows — an accident of this corpus, not a property of the design.

## Decision — the question decides, and tables get no structural floor

```
TABLE_DEPENDENT  ⟺  asked & TABLE_CUES
TABLE_CUES = {table, tabulated, row, rows, column, columns, grid}
```

`cell` removed; the fallback removed. `CLASSIFIER_VERSION` moves `question-kind-v2` →
`question-kind-v3`.

Genuine table questions are unaffected, because **a question about a table cell says "table"** —
*"Which cell of the table contains the MIC?"* still classifies `TABLE_DEPENDENT` via `table`.

### Why no structural floor, when figures have one

ADR-023 kept a narrow floor for figures: every anchor visual and none carrying text. There is no
analogue for tables, and the reason is in the builder. `table()` renders

```
caption + header rows + " | "-joined cells + linked footnotes
```

into the chunk's **own text**. A figure chunk is missing its pixels; a table chunk is missing
nothing. A table whose `header_rows` is empty is a readable label/value layout — the real example
in this corpus is a two-column disease/factor table whose first column *is* the labels — not
unreadable evidence.

A floor was specified and measured anyway (every anchor table material, and some table artifact
without header rows). It fires on **zero of 26** questions, and Options B and D produced identical
results on every one. An unfired rule carries unearned confidence, so it was not adopted.

## Decision — M8 is unchanged and remains the guarantee

Not one line of `backend/app/verification/` changed. `check_structured_evidence` still refuses any
claim citing a `TABLE`/`TABLE_PART` block whose artifacts lack `header_rows`:

```python
if any(b.chunk_type in TABLE_CHUNKS for b in cited):
    tables = [a for b in cited for a in b.artifacts if a.kind == "TABLE"]
    if not tables or not all(a.header_rows for a in tables):
        codes.append("PROVENANCE_UNRESOLVED")
```

Keyed off what the claim **cites** — already correctly scoped per ADR-013. Citations continue to
carry exact `row_indexes` and `header_rows`, and table structure is never invented.

The gate is untouched: `require_table_structure` still fires for a genuine table question, so a
headerless table or no table at all still abstains. A non-blocking `table_anchors_present` signal
was added for the same audit reason as `figure_anchors_present`.

## Consequences

On the 26-question set, `TABLE_DEPENDENT` drops from 8 to 1 (C1, which names a table explicitly)
and exactly one outcome changes: A2 (*"Is it dangerous?"*) moves from
`INSUFFICIENT / TABLE_STRUCTURE_INCOMPLETE` to reaching generation, where it should abstain via
ADR-022 declination or M8. Its abstention moves from a wrong reason to the ambiguity path ADR-021
already records as unaddressed.

**C1 stays blocked and its classification is correct.** It names the antifungal susceptibility
table, its expected answer is literally a table row, and retrieval returned four `TEXT_CHILD`
anchors and no table. That abstention is a retrieval miss surfacing correctly, not a classification
error, and it survives every option considered.

## Limitations

The gold set demonstrates only a one-question improvement; the case rests primarily on the corpus
frequency measurement and the six non-gold questions. That is weaker gold-set evidence than the
figure change had, and it is why the corpus measurement is recorded here rather than summarised.

Removing the fallback is a genuine loosening: a question that truly needs table structure but names
no cue — *"Compare the MICs for these three agents"* — now reaches generation with only the
per-claim guarantee behind it rather than a whole-question one. That is the same trade ADR-023
made, for the same reason, and it is a trade rather than a free win.
