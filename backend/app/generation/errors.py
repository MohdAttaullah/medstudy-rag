"""Declared generation failures. Every one of them abstains; none of them answers anyway."""

from app.core.errors import DomainError

# Whether a client may retry the identical request. Nothing here falls back to a different
# provider, a different model, or an ungrounded answer: a grounded draft that cannot be produced
# from the approved evidence is not produced at all.
RETRYABLE = frozenset(
    {
        "GENERATION_PROVIDER_UNAVAILABLE",
        "GENERATION_PROVIDER_TIMEOUT",
        "GENERATION_RATE_LIMITED",
    }
)

STATUS = {
    "GENERATION_NOT_PERMITTED": 409,
    "GENERATION_PROVIDER_UNCONFIGURED": 503,
    "GENERATION_PROVIDER_UNAVAILABLE": 503,
    "GENERATION_PROVIDER_TIMEOUT": 504,
    "GENERATION_PROVIDER_AUTH_FAILED": 502,
    "GENERATION_RATE_LIMITED": 429,
    "GENERATION_MALFORMED_RESPONSE": 502,
    "GENERATION_SCHEMA_VIOLATION": 502,
    "GENERATION_UNKNOWN_CITATION": 502,
    "GENERATION_MISSING_CITATION": 502,
    "GENERATION_EMPTY": 502,
}


class GenerationError(DomainError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(
            code,
            detail or code.replace("GENERATION_", "").replace("_", " ").capitalize() + ".",
            STATUS.get(code, 502),
        )
        self.retryable = code in RETRYABLE
