# ADR-005: Provider interfaces and explicit model selection

Status: Accepted; protocol implemented in M0, adapters planned for M7–M8.

Domain code depends on LLMProvider.generate, generate_structured and analyze_image contracts.
OpenAI and Anthropic SDK calls belong only to their future adapters. Embedding and verification
interfaces must preserve the same boundary. Do not hard-code or infer a production model identifier.

Select the strongest model from the deployment's approved, explicitly ranked capability registry.
No configured model means unavailable, not fallback to an invented model. Record exact provider,
model, prompt/schema version, generation parameters and verifier identity with each answer. Prefer
independent verifier models when evaluation and cost permit.

Provider abstraction adds schema/error normalization work and does not make vendor outputs identical.
Adapter conformance tests must cover malformed structured output, timeouts, refusals and missing vision.
M0 includes no SDK adapters or paid provider calls.
