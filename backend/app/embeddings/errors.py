from app.core.errors import DomainError

# code -> (operator-safe message, retryable)
#
# Retryable means "the same input may succeed on a later attempt". A pinned revision that does not
# match, an input that exceeds the model maximum or a vector schema that disagrees with the
# collection are deterministic: retrying them forever only hides the defect.
MESSAGES = {
    "EMBEDDING_SOURCE_NOT_READY": (
        "The active chunk dataset is unavailable or unvalidated.",
        False,
    ),
    "EMBEDDING_CONFIG_INVALID": ("The embedding configuration is invalid.", False),
    "EMBEDDING_MODEL_LOAD_FAILED": ("The embedding model could not be loaded.", True),
    "EMBEDDING_MODEL_UNAVAILABLE_OFFLINE": (
        "The pinned model is not present in the local cache and downloads are disabled.",
        False,
    ),
    "EMBEDDING_MODEL_REVISION_MISMATCH": (
        "The loaded model does not match the pinned revision.",
        False,
    ),
    "EMBEDDING_MODEL_CHECKSUM_MISMATCH": (
        "The model weights do not match the pinned checksum.",
        False,
    ),
    "EMBEDDING_TOKENIZER_MISMATCH": ("The tokenizer does not match the pinned revision.", False),
    "EMBEDDING_INPUT_TOO_LONG": (
        "A chunk exceeds the model input limit and was not truncated.",
        False,
    ),
    "EMBEDDING_INFERENCE_FAILED": ("Embedding inference failed.", True),
    "EMBEDDING_OOM": ("The embedding worker ran out of memory.", True),
    "EMBEDDING_NON_FINITE": ("A produced vector contained non-finite values.", False),
    "EMBEDDING_DIMENSION_MISMATCH": (
        "A produced vector does not have the configured dimension.",
        False,
    ),
    "EMBEDDING_PERSISTENCE_FAILED": ("Embedding metadata could not be persisted.", True),
    "EMBEDDING_TIMEOUT": ("The embedding time limit was reached.", True),
    "EMBEDDING_LEASE_EXPIRED": ("The embedding worker lease expired.", True),
    "EMBEDDING_RESOURCE_LIMIT": (
        "The chunk dataset exceeds configured embedding resource limits.",
        False,
    ),
    "VECTOR_INDEX_UNAVAILABLE": ("The vector index is unavailable.", True),
    "VECTOR_SCHEMA_MISMATCH": (
        "The vector collection schema does not match the configured representation.",
        False,
    ),
    "VECTOR_UPSERT_FAILED": ("Writing points to the vector index failed.", True),
    "VECTOR_RECONCILIATION_FAILED": (
        "The vector index does not reconcile with the recorded embeddings.",
        False,
    ),
    "VECTOR_ACTIVATION_FAILED": ("The verified index could not be activated.", True),
}


class EmbeddingError(DomainError):
    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        message, retryable = MESSAGES[code]
        super().__init__(code, message, 409, {"retryable": retryable, **(detail or {})})


def retryable(code: str | None) -> bool:
    return MESSAGES.get(code or "", ("", False))[1]


class EmbeddingCancelled(Exception):
    """The run was superseded, cancelled or fenced; it must not complete."""
