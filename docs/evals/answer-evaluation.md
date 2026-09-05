# Answer evaluation

Evaluate retrieved evidence quality separately from the generator. Human-reviewed atomic claims
and deterministic citation resolution form the reference, not another LLM's confidence alone.

Record answer correctness, supported claim fraction, unsupported material claim rate, citation
correctness (ID plus source/version/page/box), evidence coverage and conflict handling. Abstention
precision is warranted abstentions / all abstentions; recall is warranted abstentions / all cases
that should abstain. Report zero-denominator metrics as undefined with counts.

Include insufficient corpus evidence, competing authoritative sources, incorrect assessment keys,
generated-caption-only retrieval, missing images, OCR uncertainty, prompt injection and patient-specific
requests. Verify that answer text cannot hide claims outside the declared claim list. Final output
must not leak unverified streamed claims. Persist corpus/config/prompt/model versions, timing and cost.

Thresholds and launch criteria require held-out expert review. M0 provides synthetic acceptance
fixtures only; it has no answer evaluator and makes no measured medical accuracy claim.
