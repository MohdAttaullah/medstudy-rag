"""Parser error taxonomy and retryability.

Deterministic input failures (a corrupt or unsupported PDF) are never retried: the same bytes
produce the same failure, so retrying only hides the defect. Environmental failures (storage,
timeout, resource exhaustion) are retryable within the job's existing bounded retry budget.
"""

from dataclasses import dataclass

PARSER_SOURCE_MISSING = "PARSER_SOURCE_MISSING"
PARSER_SOURCE_CORRUPT = "PARSER_SOURCE_CORRUPT"
PARSER_UNSUPPORTED_PDF = "PARSER_UNSUPPORTED_PDF"
PARSER_PAGE_LIMIT_EXCEEDED = "PARSER_PAGE_LIMIT_EXCEEDED"
PARSER_TIMEOUT = "PARSER_TIMEOUT"
PARSER_OOM = "PARSER_OOM"
PARSER_INTERNAL_ERROR = "PARSER_INTERNAL_ERROR"
PARSER_UNAVAILABLE = "PARSER_UNAVAILABLE"
PARSER_LEASE_EXPIRED = "PARSER_LEASE_EXPIRED"
OCR_FAILED = "OCR_FAILED"
NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
NORMALIZATION_INVALID_STRUCTURE = "NORMALIZATION_INVALID_STRUCTURE"
RAW_ARTIFACT_STORAGE_FAILED = "RAW_ARTIFACT_STORAGE_FAILED"
FIGURE_ARTIFACT_STORAGE_FAILED = "FIGURE_ARTIFACT_STORAGE_FAILED"
PAGE_PREVIEW_STORAGE_FAILED = "PAGE_PREVIEW_STORAGE_FAILED"
PARSE_PERSISTENCE_FAILED = "PARSE_PERSISTENCE_FAILED"
PARSE_VALIDATION_FAILED = "PARSE_VALIDATION_FAILED"
PARSE_NEEDS_REVIEW = "PARSE_NEEDS_REVIEW"

RETRYABLE: frozenset[str] = frozenset(
    {
        PARSER_TIMEOUT,
        PARSER_OOM,
        PARSER_UNAVAILABLE,
        PARSER_LEASE_EXPIRED,
        PARSER_INTERNAL_ERROR,
        OCR_FAILED,
        RAW_ARTIFACT_STORAGE_FAILED,
        FIGURE_ARTIFACT_STORAGE_FAILED,
        PARSE_PERSISTENCE_FAILED,
    }
)
TERMINAL: frozenset[str] = frozenset(
    {
        PARSER_SOURCE_MISSING,
        PARSER_SOURCE_CORRUPT,
        PARSER_UNSUPPORTED_PDF,
        PARSER_PAGE_LIMIT_EXCEEDED,
        NORMALIZATION_FAILED,
        NORMALIZATION_INVALID_STRUCTURE,
        PARSE_VALIDATION_FAILED,
        PARSE_NEEDS_REVIEW,
        PAGE_PREVIEW_STORAGE_FAILED,
    }
)

SAFE_MESSAGES: dict[str, str] = {
    PARSER_SOURCE_MISSING: "The original file could not be retrieved for parsing.",
    PARSER_SOURCE_CORRUPT: "The original file could not be read as a structured document.",
    PARSER_UNSUPPORTED_PDF: "This document uses features the parser does not support.",
    PARSER_PAGE_LIMIT_EXCEEDED: "The document exceeds the configured parser page limit.",
    PARSER_TIMEOUT: "Parsing exceeded its configured time limit.",
    PARSER_OOM: "Parsing exceeded the available worker memory.",
    PARSER_INTERNAL_ERROR: "The parser failed while processing this document.",
    PARSER_UNAVAILABLE: "The document parser is not installed in this worker.",
    PARSER_LEASE_EXPIRED: "A parse attempt stopped responding and was released.",
    OCR_FAILED: "Optical character recognition failed for this document.",
    NORMALIZATION_FAILED: "Parsed output could not be normalized.",
    NORMALIZATION_INVALID_STRUCTURE: "Parsed output failed structural integrity checks.",
    RAW_ARTIFACT_STORAGE_FAILED: "The raw parse artifact could not be stored.",
    FIGURE_ARTIFACT_STORAGE_FAILED: "An extracted figure could not be stored.",
    PAGE_PREVIEW_STORAGE_FAILED: "A page preview could not be stored.",
    PARSE_PERSISTENCE_FAILED: "Parsed structure could not be persisted.",
    PARSE_VALIDATION_FAILED: "Parse quality validation rejected this document.",
    PARSE_NEEDS_REVIEW: "Parse quality requires human review before further processing.",
}


def is_retryable(code: str) -> bool:
    """Unknown codes are treated as non-retryable so an unclassified defect stays visible."""
    return code in RETRYABLE


def safe_message(code: str) -> str:
    return SAFE_MESSAGES.get(code, "Parsing failed.")


@dataclass
class ParserError(Exception):
    """Raised by parser adapters and pipeline stages. Carries no source text or vendor detail."""

    code: str
    detail: str | None = None

    def __post_init__(self) -> None:
        super().__init__(self.code)

    @property
    def message(self) -> str:
        return safe_message(self.code)

    @property
    def retryable(self) -> bool:
        return is_retryable(self.code)
