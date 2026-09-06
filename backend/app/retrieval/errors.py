"""Declared retrieval failures.

Retryable means "the same request may succeed on a later attempt". A pinned encoder revision that
does not match, a query longer than the model accepts, or two lanes built from different corpus
versions are deterministic: retrying them forever only hides the defect.

No failure here falls back to a different model, a different lane or a different corpus version.
Substituting an encoder or quietly dropping a lane would change what the ranking means without
anyone being told, which is exactly the class of silent architecture change this project forbids.
"""

from app.core.errors import DomainError

MESSAGES = {
    # Query encoding
    "QUERY_ENCODER_UNAVAILABLE": ("The query encoder is unavailable.", True),
    "QUERY_ENCODER_REVISION_MISMATCH": (
        "The loaded query encoder does not match the pinned revision.",
        False,
    ),
    "QUERY_ENCODER_CHECKSUM_MISMATCH": (
        "The query encoder files do not match the pinned checksums.",
        False,
    ),
    "QUERY_ENCODER_UNAVAILABLE_OFFLINE": (
        "The pinned query encoder is not present in the local cache and downloads are disabled.",
        False,
    ),
    "QUERY_TOO_LONG": (
        "The question exceeds the supported query length and was not truncated.",
        False,
    ),
    "QUERY_EMPTY": ("The question is empty after normalization.", False),
    "QUERY_ENCODING_FAILED": ("Encoding the question failed.", True),
    "QUERY_VECTOR_INVALID": ("The encoder produced an unusable query vector.", False),
    # Dense lane
    "DENSE_INDEX_NOT_READY": ("No verified dense index is available for this tenant.", False),
    "DENSE_SEARCH_FAILED": ("Dense search failed.", True),
    "RETRIEVAL_VECTOR_SPACE_MISMATCH": (
        "The query encoder and the stored vectors are not in the same vector space.",
        False,
    ),
    # Sparse lane
    "SPARSE_INDEX_NOT_READY": ("No verified lexical index is available for this tenant.", False),
    "SPARSE_INDEX_VERSION_MISMATCH": (
        "The lexical index was built by a different analyzer version.",
        False,
    ),
    "SPARSE_SEARCH_FAILED": ("Lexical search failed.", True),
    "SPARSE_QUERY_TOO_BROAD": (
        "The lexical query matches more postings than the configured scan limit.",
        False,
    ),
    # Corpus consistency
    "RETRIEVAL_CORPUS_MISALIGNED": (
        "The dense and lexical indexes were built from different chunk datasets.",
        False,
    ),
    "RETRIEVAL_CORPUS_EMPTY": ("No document version is ready for retrieval.", False),
    "RETRIEVAL_MODE_UNAVAILABLE": (
        "The configured retrieval mode cannot run and fallback is disabled.",
        False,
    ),
    "RETRIEVAL_TIMEOUT": ("The retrieval time limit was reached.", True),
    "RETRIEVAL_HYDRATION_FAILED": ("Retrieved candidates could not be resolved to sources.", True),
    # Sparse index build
    "SPARSE_SOURCE_NOT_READY": (
        "The active chunk dataset is unavailable or unvalidated.",
        False,
    ),
    "SPARSE_BUILD_FAILED": ("The lexical index could not be built.", True),
    "SPARSE_RECONCILIATION_FAILED": (
        "The lexical index does not reconcile with the recorded chunks.",
        False,
    ),
    "SPARSE_PERSISTENCE_FAILED": ("Lexical index metadata could not be persisted.", True),
    "SPARSE_RESOURCE_LIMIT": (
        "The chunk dataset exceeds configured lexical index resource limits.",
        False,
    ),
    "SPARSE_TIMEOUT": ("The lexical index time limit was reached.", True),
    "SPARSE_LEASE_EXPIRED": ("The lexical index worker lease expired.", True),
}


class RetrievalError(DomainError):
    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        message, retryable = MESSAGES[code]
        super().__init__(code, message, 409, {"retryable": retryable, **(detail or {})})


def retryable(code: str | None) -> bool:
    return MESSAGES.get(code or "", ("", False))[1]


class SparseIndexCancelled(Exception):
    """The build was superseded, cancelled or fenced; it must not complete."""
