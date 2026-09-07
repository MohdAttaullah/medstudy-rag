# M6 evidence construction

EvidenceSet is a source inspection contract, not proof of answer sufficiency. It contains query
hash, M5 trace, model/policy/timing trace, anchors, expansions, ordered blocks, total tokens,
visual-source flag, duplicate count, warnings and `answering_enabled=false`.

Every block carries stable identity, anchor, contributing chunks and source elements, source
offsets, page/bounding boxes, document/version/chunk-run/parse-run IDs, source type, authority,
hierarchy, artifacts, reasons, exact text representation and token count. Atomic questions list
all contributing chunks. A figure with no source caption remains linked to its original artifact.

Default budget: 4096 tokens, 20 blocks, 1024 tokens per block. The pinned bundled WordPiece
tokenizer counts exact block text including structural labels. This is an inspection budget,
not a future generation model's prompt budget. Ranked anchors are admitted before optional
context. Atomic units that cannot fit are omitted whole with `CONTEXT_BUDGET_EXCEEDED`;
no expansion survives without its anchor. Context is displayed adjacent to its anchor in source order.

- Parent: same tenant/version/active ChunkRun; incomplete text fragments only; already covered
  source intervals removed and the remainder bounded to 512 tokens.
- Neighbour: at most one each side, same non-null parent and run, source-order continuity,
  256 tokens maximum. No global window across sections.
- Table: selected M3 part, canonical headers, caption and explicitly linked footnotes. Selected
  cells are validated against TableArtifact; row/header IDs and source links remain available.
  A large table is never expanded to all rows automatically.
- Formula: exact expression with M2/M3-associated context. No generated correction or definitions.
- Figure: original FigureArtifact, source caption/context and crop availability, without interpretation.
- Question: source question, options, explicit answer only when present, and source explanation
  stay atomic with assessment authority. Oversized records are omitted, not silently split.

Deduplication uses chunk IDs, source intervals within document version, table artifact/row identity,
and visual artifact identity. Necessary headers can repeat across distinct table parts. Independent
documents/versions and competing source values are preserved; no semantic-similarity deduplication
or model-based conflict resolution exists.

Every lookup reapplies lineage. Missing required provenance/artifacts or corpus drift fails closed.
Final assembly uses the verified immutable snapshot. Optional context omitted by size or source
continuity policy yields findings. Evaluation prescribes anchors separately from retrieval and
reports source-element and source-character coverage. One element can contain many table rows or
paragraph chunks; neither metric is a sufficiency gate. Missing/noisy context remains in the report.
