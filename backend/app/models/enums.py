from enum import StrEnum


class SourceType(StrEnum):
    GUIDELINE = "GUIDELINE"
    REFERENCE_BOOK = "REFERENCE_BOOK"
    TEXTBOOK = "TEXTBOOK"
    COURSE_MATERIAL = "COURSE_MATERIAL"
    QUESTION_BANK = "QUESTION_BANK"
    QUESTION_PAPER = "QUESTION_PAPER"
    ANSWER_KEY = "ANSWER_KEY"
    OTHER = "OTHER"


class Authority(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    ASSESSMENT = "ASSESSMENT"
    REFERENCE = "REFERENCE"
    HIGH = "HIGH"


class Status(StrEnum):
    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    NORMALIZING = "NORMALIZING"
    ENRICHING = "ENRICHING"
    READY_FOR_CHUNKING = "READY_FOR_CHUNKING"
    VALIDATING_CHUNKS = "VALIDATING_CHUNKS"
    READY_FOR_EMBEDDING = "READY_FOR_EMBEDDING"
    READY_FOR_RETRIEVAL = "READY_FOR_RETRIEVAL"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    VERIFYING_INDEX = "VERIFYING_INDEX"
    READY = "READY"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CANCELLED = "CANCELLED"


# Ordered exactly as the M1 migration declared them; the persisted CHECK constraint is a set,
# but keeping one literal list avoids a drifting duplicate between migrations and the model.
STATUS_VALUES: tuple[str, ...] = tuple(status.value for status in Status)


class EmbeddingRunStatus(StrEnum):
    """Lifecycle of one durable embedding attempt, independent of the owning job status."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CANCELLED = "CANCELLED"


class IndexRunStatus(StrEnum):
    """Lifecycle of one durable index load. Only VERIFIED is eligible to become active."""

    STAGING = "STAGING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class ParseRunStatus(StrEnum):
    """Lifecycle of one durable parse attempt, independent of the owning job status."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ParseResult(StrEnum):
    """Structured outcome of the deterministic parse-quality validation layer."""

    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAIL = "FAIL"


class ElementType(StrEnum):
    TITLE = "TITLE"
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST = "LIST"
    LIST_ITEM = "LIST_ITEM"
    TABLE = "TABLE"
    FORMULA = "FORMULA"
    FIGURE = "FIGURE"
    CAPTION = "CAPTION"
    FOOTNOTE = "FOOTNOTE"
    PAGE_HEADER = "PAGE_HEADER"
    PAGE_FOOTER = "PAGE_FOOTER"
    SECTION = "SECTION"
    CODE = "CODE"
    OTHER = "OTHER"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class OcrMode(StrEnum):
    """OFF never rasterizes; AUTO lets the parser OCR only regions without a text layer."""

    OFF = "OFF"
    AUTO = "AUTO"
    FORCE = "FORCE"


class CoordinateOrigin(StrEnum):
    TOPLEFT = "TOPLEFT"
