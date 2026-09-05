# ADR-001: Preserve Docling structure and original artifacts

Status: Accepted; adapter implemented in M2 (Docling 2.126.0). See
[ADR-007](007-m2-parse-runs-and-quality-validation.md) for the parse-run, artifact-storage and
validation decisions that followed, and [document parsing](../architecture/document-parsing.md)
for the implemented contract.

Medical PDFs contain layout-dependent tables, equations, figures and assessment blocks. Choose
Docling as the initial parser, retaining its structured document JSON and parser/config version.
Original PDFs and required crops remain the authority. Normalization creates traceable derived
elements without flattening the document hierarchy. OCR uncertainty routes to validation/review.

Rejected as primary strategy: plain-text PDF extraction plus fixed character splitting, which loses
relationships and precise provenance. Costs include larger artifacts, parser version migrations and
layout-specific tests. Benchmark Docling on representative mixed/scanned pages before production;
keep the parser interface replaceable if evidence demonstrates another parser is better.

M2 outcome: the interface is `DocumentParser` in `backend/app/ingestion/parser/model.py` and only
`docling_adapter.py` imports Docling, so the replacement cost is one module. Benchmarking against
representative real medical pages has not been done; the only measurements so far are synthetic
extraction-fidelity fixtures, and layout classification proved to vary between CPU environments.
