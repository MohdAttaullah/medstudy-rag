# Grounding and safety — target contract

Generation remains unavailable in M1. There is no model-only fallback endpoint.

The future sufficiency gate emits `SUFFICIENT`, `INSUFFICIENT` or `CONFLICTING` using calibrated
reranker scores, supporting chunks, independent sources, authority, coverage, question type and
conflict signals. Only SUFFICIENT reaches generation. Policy thresholds require held-out evaluation;
the bootstrap config deliberately supplies no invented confidence threshold.

Generator input is restricted to system grounding policy, user question, selected evidence, citation
IDs and permitted structured schema. Pretrained knowledge is not evidence. Provider adapters must
return answer, atomic claims, citations, evidence status, conflicts and abstention reason. Confidence
is a calibrated evidence signal if available, never a raw model percentage presented as medical risk.

Verify that each substantive sentence is represented in the atomic claim set, each material claim is
entailed by its cited chunks and every citation resolves to actual stored document/version/edition,
chapter, section, original page, chunk and bounding box. Deterministic provenance checks operate
independently from an LLM verifier. Prefer a different verifier model when configured and evaluated.
On material failure, at most one policy-permitted regeneration is allowed; failure then abstains.
Do not stream unverified medical text: stream progress, then only verified answer content.

Generated summaries, OCR corrections and figure descriptions carry `GENERATED_METADATA` lineage.
They never replace originals as evidence. Visually dependent questions require the retrieved original
crop/page in multimodal generation and verification; absent image support triggers abstention.
Persist image identity and transformation provenance. Text-only questions do not invoke vision.

Patient-specific diagnosis/treatment requests fall outside the product scope and require an explicit
safety response. No claim of clinical validation or HIPAA compliance is made. Conflict presentation
must identify the competing evidence and policy outcome without arbitrarily choosing a medical answer.
