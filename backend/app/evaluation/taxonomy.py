"""Which stage caused a failure, and what the declared reason codes mean.

Every code here already exists somewhere in the pipeline: the sufficiency gate, the verifier, the
evidence assembler, the retrieval services, the generation adapters or the M6 evaluation helpers.
Nothing in this module invents a new name for a condition the system already declares, because a
second name for one condition is how a taxonomy starts disagreeing with the code it describes.

The one rule that matters here is **upstream attribution**. A question that fails because the
first stage never retrieved the evidence will also, later, look like an insufficiency: the gate
correctly refuses because there is nothing to answer from. Attributing that to the gate would
credit the gate with a failure retrieval caused, and would make an end-to-end report say the
system abstains too often when what it actually does is retrieve too little. `attribute` therefore
returns the *earliest* layer that declared a code, never the last one to notice.
"""

from typing import Literal

Layer = Literal[
    "PARSING",
    "CHUNKING",
    "INDEXING",
    "RETRIEVAL",
    "RERANKING",
    "EVIDENCE",
    "SUFFICIENCY",
    "GENERATION",
    "VERIFICATION",
    "END_TO_END",
]

#: Pipeline order, upstream first. Attribution walks this list and stops at the first match.
LAYERS: tuple[Layer, ...] = (
    "PARSING",
    "CHUNKING",
    "INDEXING",
    "RETRIEVAL",
    "RERANKING",
    "EVIDENCE",
    "SUFFICIENCY",
    "GENERATION",
    "VERIFICATION",
    "END_TO_END",
)

#: Declared reason code -> the layer that owns it. Codes are the repository's own.
ATTRIBUTION: dict[str, Layer] = {
    # --- Retrieval: the evidence never entered the candidate pool at all.
    "FIRST_STAGE_MISS": "RETRIEVAL",
    "CORPUS_LACKS_EVIDENCE": "RETRIEVAL",
    "DENSE_INDEX_NOT_READY": "RETRIEVAL",
    "DENSE_SEARCH_FAILED": "RETRIEVAL",
    "QUERY_ENCODER_UNAVAILABLE": "RETRIEVAL",
    "QUERY_ENCODER_CHECKSUM_MISMATCH": "RETRIEVAL",
    # --- Reranking: the evidence was in the pool and the reranker pushed it out.
    "RERANKER_REGRESSION": "RERANKING",
    "RERANKER_NO_GAIN": "RERANKING",
    # --- Evidence assembly: retrieved, ranked, but not carried into the EvidenceSet intact.
    "CONTEXT_BUDGET_EXCEEDED": "EVIDENCE",
    "CONTEXT_INCOMPLETE": "EVIDENCE",
    "CONTEXT_INVALID_NEIGHBOUR": "EVIDENCE",
    "CONTEXT_SOURCE_LINEAGE_MISMATCH": "EVIDENCE",
    "EVIDENCE_BUDGET_OMISSION": "EVIDENCE",
    "EVIDENCE_PROVENANCE_MISSING": "EVIDENCE",
    "PROVENANCE_MISSING": "EVIDENCE",
    # Declared by the gate, but it reports a warning the assembler raised. Attributed upstream,
    # to the layer that produced the condition rather than the one that noticed it.
    "RETRIEVAL_WARNING_PRESENT": "EVIDENCE",
    # --- Sufficiency: the evidence was assembled and the gate judged it inadequate or in conflict.
    "NO_EVIDENCE": "SUFFICIENCY",
    "INSUFFICIENT_SUPPORTING_BLOCKS": "SUFFICIENCY",
    "INSUFFICIENT_INDEPENDENT_SOURCES": "SUFFICIENCY",
    "ASSESSMENT_ONLY_EVIDENCE": "SUFFICIENCY",
    "TABLE_STRUCTURE_INCOMPLETE": "SUFFICIENCY",
    "FORMULA_SOURCE_MISSING": "SUFFICIENCY",
    "VISUAL_INTERPRETATION_REQUIRED": "SUFFICIENCY",
    "VISUAL_INTERPRETATION_UNAVAILABLE": "SUFFICIENCY",
    "EVIDENCE_CONFLICT": "SUFFICIENCY",
    "AUTHORITATIVE_SOURCES_DISAGREE": "SUFFICIENCY",
    "ASSESSMENT_CONTRADICTS_REFERENCE": "SUFFICIENCY",
    "ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE": "SUFFICIENCY",
    "INDEPENDENT_SOURCE_VALUE_CONFLICT": "SUFFICIENCY",
    # --- Generation: the gate allowed a draft and the provider or its contract failed.
    "GENERATION_EMPTY": "GENERATION",
    "GENERATION_MALFORMED_RESPONSE": "GENERATION",
    "GENERATION_MISSING_CITATION": "GENERATION",
    "GENERATION_SCHEMA_VIOLATION": "GENERATION",
    "GENERATION_UNKNOWN_CITATION": "GENERATION",
    "GENERATION_FAILURE": "GENERATION",
    "GENERATION_PROVIDER_AUTH_FAILED": "GENERATION",
    "GENERATION_PROVIDER_REJECTED_REQUEST": "GENERATION",
    "GENERATION_PROVIDER_TIMEOUT": "GENERATION",
    "GENERATION_PROVIDER_UNAVAILABLE": "GENERATION",
    "GENERATION_PROVIDER_UNCONFIGURED": "GENERATION",
    "GENERATION_RATE_LIMITED": "GENERATION",
    # --- Verification: a draft existed and claim checking refused it. Every member of the real
    # `ReasonCode` literal appears here; `test_every_verification_reason_code_is_classified`
    # fails if M8 ever declares one this module has not been taught.
    "CLAIM_NOT_CITED": "VERIFICATION",
    "UNKNOWN_CITATION": "VERIFICATION",
    "PROVENANCE_UNRESOLVED": "VERIFICATION",
    "TENANT_SCOPE_VIOLATION": "VERIFICATION",
    "NUMERIC_MISMATCH": "VERIFICATION",
    "UNIT_MISMATCH": "VERIFICATION",
    "NEGATION_REVERSED": "VERIFICATION",
    "OVERSTATED_CERTAINTY": "VERIFICATION",
    "SEMANTICALLY_UNSUPPORTED": "VERIFICATION",
    "SEMANTICALLY_CONTRADICTED": "VERIFICATION",
    "SEMANTIC_EVIDENCE_INSUFFICIENT": "VERIFICATION",
    "CONTRADICTED_BY_RETAINED_EVIDENCE": "VERIFICATION",
    "UNSUPPORTED_CLAIM": "VERIFICATION",
    "CONTRADICTED_CLAIM": "VERIFICATION",
    "CITATION_INVALID": "VERIFICATION",
    "VERIFIER_FAILED": "VERIFICATION",
    "VERIFIER_MALFORMED_OUTPUT": "VERIFICATION",
    "VERIFIER_UNKNOWN_EVIDENCE": "VERIFICATION",
    "VERIFICATION_UNAVAILABLE": "VERIFICATION",
}

#: Codes that report success. They are classified so nothing is "unknown", but they are never
#: attributed to a failing layer: counting a support code as a failure would be worse than
#: leaving it unclassified.
SUCCESS: frozenset[str] = frozenset(
    {
        "SUPPORTED_BY_CITED_EVIDENCE",
        "NON_MATERIAL_CLAIM",
        "SUPPORTED_BY_SOURCE_EVIDENCE",
        "SUPPORTED_BY_NON_ASSESSMENT_SOURCE",
        "SUPPORTED_BY_INDEPENDENT_SOURCES",
        "REQUIRED_ARTIFACT_PRESENT",
    }
)

#: Codes that mean the infrastructure broke rather than that the evidence was inadequate. An
#: outage must never be reported as a statement about the corpus.
INFRASTRUCTURE: frozenset[str] = frozenset(
    {
        "DENSE_INDEX_NOT_READY",
        "DENSE_SEARCH_FAILED",
        "QUERY_ENCODER_UNAVAILABLE",
        "QUERY_ENCODER_CHECKSUM_MISMATCH",
        "GENERATION_PROVIDER_AUTH_FAILED",
        "GENERATION_PROVIDER_REJECTED_REQUEST",
        "GENERATION_PROVIDER_TIMEOUT",
        "GENERATION_PROVIDER_UNAVAILABLE",
        "GENERATION_PROVIDER_UNCONFIGURED",
        "GENERATION_RATE_LIMITED",
        "GENERATION_MALFORMED_RESPONSE",
        "VERIFIER_FAILED",
        "VERIFIER_MALFORMED_OUTPUT",
        "VERIFICATION_UNAVAILABLE",
    }
)

#: Codes that describe a safe refusal rather than a defect. A corpus that genuinely lacks an
#: answer producing an abstention is the system working, and must not inflate a failure count.
CORRECT_REFUSAL: frozenset[str] = frozenset(
    {
        "NO_EVIDENCE",
        "CORPUS_LACKS_EVIDENCE",
        "EVIDENCE_CONFLICT",
        "AUTHORITATIVE_SOURCES_DISAGREE",
        "ASSESSMENT_CONTRADICTS_REFERENCE",
        "ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE",
        "ASSESSMENT_ONLY_EVIDENCE",
        "INDEPENDENT_SOURCE_VALUE_CONFLICT",
        "VISUAL_INTERPRETATION_REQUIRED",
        "VISUAL_INTERPRETATION_UNAVAILABLE",
    }
)


def attribute(codes: object) -> Layer | None:
    """The earliest layer that declared one of these codes, or None if none is recognised.

    Upstream wins deliberately. A first-stage miss reported alongside an insufficiency is a
    retrieval failure the gate then handled correctly, not a gate failure.
    """
    known = {code for code in _codes(codes) if code in ATTRIBUTION and code not in SUCCESS}
    if not known:
        return None
    owners = {ATTRIBUTION[code] for code in known}
    return next(layer for layer in LAYERS if layer in owners)


def unknown_codes(codes: object) -> tuple[str, ...]:
    """Codes the taxonomy does not classify.

    Reported rather than ignored: an unclassified code means the pipeline declares something this
    module has not been taught, and silently dropping it would understate failure attribution.
    """
    return tuple(
        sorted(code for code in _codes(codes) if code not in ATTRIBUTION and code not in SUCCESS)
    )


def is_infrastructure(codes: object) -> bool:
    return any(code in INFRASTRUCTURE for code in _codes(codes))


def _codes(codes: object) -> tuple[str, ...]:
    if codes is None:
        return ()
    if isinstance(codes, str):
        return (codes,)
    if isinstance(codes, dict):
        return tuple(str(code) for code in codes)
    if isinstance(codes, (list, tuple, set, frozenset)):
        return tuple(str(code) for code in codes)
    return (str(codes),)
