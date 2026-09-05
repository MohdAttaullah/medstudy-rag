# ADR-001: Preserve Docling structure and original artifacts

Status: Accepted for target architecture; adapter scheduled for M2.

Medical PDFs contain layout-dependent tables, equations, figures and assessment blocks. Choose
Docling as the initial parser, retaining its structured document JSON and parser/config version.
Original PDFs and required crops remain the authority. Normalization creates traceable derived
elements without flattening the document hierarchy. OCR uncertainty routes to validation/review.

Rejected as primary strategy: plain-text PDF extraction plus fixed character splitting, which loses
relationships and precise provenance. Costs include larger artifacts, parser version migrations and
layout-specific tests. Benchmark Docling on representative mixed/scanned pages before production;
keep the parser interface replaceable if evidence demonstrates another parser is better.
