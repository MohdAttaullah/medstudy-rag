from app.core.errors import DomainError

MESSAGES = {
    "CHUNK_SOURCE_PARSE_NOT_READY": (
        "The active source parse is unavailable or unvalidated.",
        False,
    ),
    "CHUNK_CONFIG_INVALID": ("The chunking configuration is invalid.", False),
    "CHUNK_TOKENIZER_LOAD_FAILED": ("The pinned local tokenizer could not be loaded.", False),
    "CHUNK_NORMALIZATION_FAILED": ("The normalized source is inconsistent.", False),
    "CHUNK_TABLE_FAILED": ("A source table has invalid canonical structure.", False),
    "CHUNK_FORMULA_FAILED": ("A source formula has no expression.", False),
    "CHUNK_QUESTION_FAILED": ("Question boundaries require review.", False),
    "CHUNK_PERSISTENCE_FAILED": ("The chunk dataset could not be persisted.", True),
    "CHUNK_VALIDATION_FAILED": ("The chunk dataset failed integrity validation.", False),
    "CHUNK_NEEDS_REVIEW": ("Chunk quality findings require review.", False),
    "CHUNK_TIMEOUT": ("The chunking time limit was reached.", True),
    "CHUNK_LEASE_EXPIRED": ("The chunking worker lease expired.", True),
    "CHUNK_RESOURCE_LIMIT": ("The source exceeds configured chunking resource limits.", False),
}


class ChunkError(DomainError):
    def __init__(self, code: str) -> None:
        message, retryable = MESSAGES[code]
        super().__init__(code, message, 409, {"retryable": retryable})


def retryable(code: str | None) -> bool:
    return MESSAGES.get(code or "", ("", False))[1]


class ChunkCancelled(Exception):
    pass
