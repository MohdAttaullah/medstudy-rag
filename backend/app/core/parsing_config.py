"""Typed, frozen M2 parsing policy. Snapshotted per ParseRun; never mutated in place."""

import hashlib
import json
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    timeout_seconds: int = Field(default=900, ge=10, le=14400)
    max_pages: int = Field(default=2000, ge=1, le=20000)
    #: Ceiling on the whole ingestion task, not just the parser call. `timeout_seconds` bounds
    #: Docling; this bounds everything around it — download from object storage, normalisation,
    #: artifact upload — so a task that stalls outside the parser cannot hold the single worker
    #: slot forever. Must exceed `timeout_seconds` or the parser would never get to finish.
    task_timeout_seconds: int = Field(default=1800, ge=60, le=21600)
    #: Grace period before the hard kill, so the task can record a failure state rather than
    #: vanishing and leaving a job row stuck in a running state.
    task_soft_timeout_seconds: int = Field(default=1500, ge=30, le=21000)
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
    def timeouts_are_ordered(self) -> Self:
        """Parser timeout < soft task limit < hard task limit.

        Out of order, the outer limit fires first and the parser never reaches its own timeout,
        so a document that would have failed with a diagnosable parse error is killed by the
        worker instead and the reason is lost. Enforced here rather than left to defaults,
        because these are three independently configurable values.
        """
        if self.task_soft_timeout_seconds <= self.timeout_seconds:
            raise ValueError(
                "task_soft_timeout_seconds must exceed timeout_seconds so the parser can fail "
                "with its own diagnosable error before the worker intervenes"
            )
        if self.task_timeout_seconds <= self.task_soft_timeout_seconds:
            raise ValueError(
                "task_timeout_seconds must exceed task_soft_timeout_seconds so a task has a "
                "grace period to record a failure before it is killed"
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
