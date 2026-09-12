"""Typed, frozen M2 parsing policy. Snapshotted per ParseRun; never mutated in place."""

import hashlib
import json
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ingestion.parser.model import document_budget_seconds
from app.models.enums import OcrMode


class ParseThresholds(BaseModel):
    """Deterministic quality bounds. Every value is a measurable ratio or count, not an accuracy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_non_empty_page_ratio: float = Field(default=0.6, ge=0, le=1)
    min_chars_per_page: int = Field(default=40, ge=0, le=10000)
    min_elements_per_page: float = Field(default=0.5, ge=0, le=100)
    max_invalid_bbox_ratio: float = Field(default=0.02, ge=0, le=1)
    max_unlocated_element_ratio: float = Field(default=0.10, ge=0, le=1)
    max_malformed_table_ratio: float = Field(default=0.25, ge=0, le=1)
    max_suspicious_ocr_page_ratio: float = Field(default=0.25, ge=0, le=1)
    max_empty_formula_ratio: float = Field(default=0.25, ge=0, le=1)
    # A page whose source text layer is empty but which the parser also left empty is the
    # signal that content was lost; it is reported, never silently accepted.
    review_on_page_count_mismatch: bool = True


class ParsingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="parsing-m2-v1", min_length=1, max_length=80)
    parser_name: str = Field(default="docling", min_length=1, max_length=60)
    #: Budget for a single Docling conversion call. For a document short enough to convert
    #: in one call that is the whole document; for a windowed conversion it bounds one
    #: window. The whole-document ceiling is `document_timeout_for`, which has to scale with
    #: length because a real 932-page textbook measured ~5.7 s/page and no fixed value can
    #: be right for both it and a one-page leaflet.
    timeout_seconds: int = Field(default=900, ge=10, le=14400)
    max_pages: int = Field(default=2000, ge=1, le=20000)
    #: Pages converted per Docling call for a long document. Measured on this host, a single
    #: whole-book conversion retains roughly 16-18 MiB per page inside Docling and grows
    #: linearly: 200 pages reached 3.7 GiB, and a ~340-page textbook reached 6.2 GiB and was
    #: SIGKILLed. Converting in windows lets each window's page state be released, and measured
    #: RSS then plateaus near 1.5 GiB regardless of book length.
    #:
    #: Windows are joined by absolute page number, which Docling preserves under `page_range`,
    #: so provenance is unchanged. 0 disables windowing entirely.
    page_window_size: int = Field(default=25, ge=0, le=500)
    #: Documents at or below this page count are converted in a single call, exactly as
    #: before. It equals `page_window_size`, so no conversion call ever handles more pages
    #: than one window and the memory bound is uniform: a single 50-page call on real
    #: textbook content measured 2442 MiB, while windowed conversion plateaus near 1.4 GiB.
    #: Every fixture in this repository is 3 pages or fewer, so existing parse output — and
    #: the determinism the M2 evaluation asserts — is untouched; windowing takes over only
    #: where the single-call path is the thing that fails.
    page_window_threshold: int = Field(default=25, ge=1, le=20000)
    #: Per-page share of the whole-document budget. Measured on this host, dense textbook
    #: pages with tables and figures cost ~5.7 s each; the default is roughly double that,
    #: so a slower host or a heavier page does not turn a healthy parse into a timeout.
    document_seconds_per_page: float = Field(default=12.0, ge=0.1, le=120)
    #: Ceiling on the whole-document budget however long the book is. This is what stops an
    #: unbounded parse, not a fixed per-document timeout.
    max_document_timeout_seconds: int = Field(default=14400, ge=60, le=21600)
    #: Ceiling on the whole ingestion task, not just the parser call. The parse budget bounds
    #: Docling; this bounds everything around it — download from object storage, normalisation,
    #: artifact upload — so a task that stalls outside the parser cannot hold the single worker
    #: slot forever.
    task_timeout_seconds: int = Field(default=15900, ge=60, le=21600)
    #: Grace period before the hard kill, so the task can record a failure state rather than
    #: vanishing and leaving a job row stuck in a running state.
    task_soft_timeout_seconds: int = Field(default=15600, ge=30, le=21000)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    # Pinned parser thread count: reduction order affects layout prediction, so leaving this to
    # the host CPU count would make the same document parse differently on different machines.
    parser_threads: int = Field(default=4, ge=1, le=32)
    ocr_mode: OcrMode = OcrMode.AUTO
    extract_tables: bool = True
    extract_formulas: bool = True
    extract_figures: bool = True
    generate_page_previews: bool = True
    preview_scale: float = Field(default=1.5, ge=0.5, le=4)
    preview_format: str = Field(default="webp", pattern="^(webp|png)$")
    figure_format: str = Field(default="png", pattern="^(png|webp)$")
    max_artifact_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    temp_dir: str | None = None
    lease_seconds: int = Field(default=1800, ge=60, le=28800)
    thresholds: ParseThresholds = ParseThresholds()

    @model_validator(mode="after")
    def windowing_is_coherent(self) -> Self:
        if self.page_window_size and self.page_window_size > self.page_window_threshold:
            raise ValueError(
                "page_window_size must not exceed page_window_threshold, or the first window "
                "would already be larger than the document that triggered windowing"
            )
        return self

    def document_timeout_for(self, pages: int) -> int:
        """Whole-document parse budget for a document of `pages` pages.

        A fixed budget cannot be right for a corpus holding both a one-page leaflet and a
        932-page textbook: the value that lets the book finish would let a stuck one-page
        parse hold the worker for hours. The budget therefore scales with the work and is
        capped, the same shape as the upload validation budget in `IngestionConfig`.
        """
        return document_budget_seconds(
            pages,
            self.timeout_seconds,
            self.document_seconds_per_page,
            self.max_document_timeout_seconds,
        )

    @model_validator(mode="after")
    def timeouts_are_ordered(self) -> Self:
        """One call < whole document < soft task limit < hard task limit.

        Out of order, the outer limit fires first and the parser never reaches its own
        timeout, so a document that would have failed with a diagnosable parse error is
        killed by the worker instead and the reason is lost. The soft limit has to clear the
        document budget *plus* one conversion call, because the budget is checked between
        windows: a parse already over budget still finishes the window it is in before it
        can report the timeout. Enforced here rather than left to defaults, because these
        are independently configurable values.
        """
        if self.max_document_timeout_seconds < self.timeout_seconds:
            raise ValueError(
                "max_document_timeout_seconds must be at least timeout_seconds, or a "
                "document would be given less time than one conversion call is allowed"
            )
        overshoot = self.max_document_timeout_seconds + self.timeout_seconds
        if self.task_soft_timeout_seconds <= overshoot:
            raise ValueError(
                "task_soft_timeout_seconds must exceed max_document_timeout_seconds plus "
                "timeout_seconds so the parser can fail with its own diagnosable error "
                "before the worker intervenes"
            )
        if self.task_timeout_seconds <= self.task_soft_timeout_seconds:
            raise ValueError(
                "task_timeout_seconds must exceed task_soft_timeout_seconds so a task has a "
                "grace period to record a failure before it is killed"
            )
        return self

    @model_validator(mode="after")
    def the_lease_outlives_one_conversion_call(self) -> Self:
        """The lease must cover the longest gap between heartbeats.

        The parse lease is renewed at stage boundaries and between page windows, so the
        longest interval during which nothing can renew it is one conversion call. A lease
        shorter than that would let the reaper fail a healthy parse out from under itself.
        """
        if self.lease_seconds <= self.timeout_seconds:
            raise ValueError(
                "lease_seconds must exceed timeout_seconds: nothing renews the lease during "
                "a conversion call, so a shorter lease would reap a healthy parse"
            )
        return self

    @property
    def fingerprint(self) -> str:
        """Content digest of the whole policy.

        Reparse idempotency keys on this, not on `version`, so a silently edited threshold
        cannot reuse a parse produced under different rules.
        """
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
