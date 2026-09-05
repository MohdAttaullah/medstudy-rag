# ADR-004: Fail closed on insufficient or unsupported evidence

Status: Accepted safety contract; gate/verifier implementations scheduled for M7–M8.

The LLM is not a medical source. Permit generation only after evidence sufficiency. Check atomic
claims and real citation provenance after generation; at most one configured repair attempt precedes
abstention. Material authoritative conflicts are reported or abstained from, never silently chosen.
Assessment keys and generated enrichment cannot override source authority.

Original images must reach multimodal verification when the answer depends on interpreting them.
Stream progress first, then verified content. Pre-verification token streaming is rejected because
it can expose claims that later fail. This costs latency and answer coverage in exchange for explicit
uncertainty. Evaluate unsupported claim rate, citation correctness and abstention behaviour separately.
No prompt alone is considered a proof of grounding.
