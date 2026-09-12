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
    RETRIEVAL_READY = "RETRIEVAL_READY"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    VERIFYING_INDEX = "VERIFYING_INDEX"
    SPARSE_INDEXING = "SPARSE_INDEXING"
    VERIFYING_SPARSE_INDEX = "VERIFYING_SPARSE_INDEX"
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


class SparseIndexStatus(StrEnum):
    """Lifecycle of one durable lexical index build. Only VERIFIED is eligible to become active."""

    STAGING = "STAGING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class RetrievalMode(StrEnum):
    """First-stage strategies. They exist so lane quality can be compared, not as user settings."""

    DENSE_ONLY = "DENSE_ONLY"
    BM25_ONLY = "BM25_ONLY"
    HYBRID_RRF = "HYBRID_RRF"


class ParseRunStatus(StrEnum):
    """Lifecycle of one durable parse attempt, independent of the owning job status.

    `REVIEWED_ACCEPTED` is a parse the deterministic quality layer flagged and an authorized
    curator then accepted for downstream ingestion. It is deliberately *not* `SUCCEEDED`: the
    distinction between automatic success and human judgement has to survive in the record.
    Such a run keeps `validation_result = NEEDS_REVIEW` forever and keeps every finding.

    Downstream eligibility therefore cannot be decided from `validation_result` alone. Anything
    gating on "is this parse usable" must consult this status as well, in the database guards as
    much as in Python.
    """

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    REVIEWED_ACCEPTED = "REVIEWED_ACCEPTED"
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
