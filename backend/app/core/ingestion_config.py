from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IngestionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = Field(default="ingestion-m1-v1", min_length=1, max_length=80)
    #: Per **file**, and per HTTP request: the browser sends one PDF as the whole request body,
    #: never a multipart batch, so this is both limits at once. Raised from 128 MiB in the
    #: post-M12 large-document work after a real 153 MiB textbook was rejected; medical
    #: textbooks in the intended corpus reach 300-500 MB.
    #:
    #: The reverse proxy must allow at least this much or it rejects the request before the
    #: application ever sees it. `MEDRAG_MAX_UPLOAD_MB` in compose drives both, and
    #: `scripts/production_preflight.py` fails when they disagree.
    #:
    #: The 1 GiB ceiling is deliberate. Uploading is bounded-memory, but parsing is not: a
    #: document large enough to exhaust the worker must be refused at the door rather than
    #: accepted and then killed halfway through ingestion.
    max_upload_bytes: int = Field(default=512 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    allowed_mime_types: tuple[Literal["application/pdf"], ...] = ("application/pdf",)
    duplicate_policy: Literal["reject-within-tenant"] = "reject-within-tenant"
    max_retries: int = Field(default=3, ge=0, le=10)
    stream_chunk_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)
    #: Base budget for the sandboxed structural check. A flat 20 s was enough for a 2 MiB
    #: fixture and far too little for a 153 MiB textbook, which timed out during acceptance
    #: testing. Keeping a small base means a hostile *small* PDF still fails fast rather than
    #: being granted the allowance a large one needs.
    validation_timeout_seconds: int = Field(default=30, ge=1, le=600)
    #: Additional seconds granted per MiB. Scaling with size is what lets one setting serve both
    #: a 1 MiB leaflet and a 512 MiB textbook without either being mistreated.
    validation_seconds_per_mib: float = Field(default=0.6, ge=0, le=10)
    #: Hard ceiling regardless of size, so a pathological document cannot occupy the request
    #: path indefinitely. Validation runs in a subprocess and is killed at this bound.
    validation_timeout_max_seconds: int = Field(default=600, ge=5, le=3600)
    #: Wall clock for receiving one upload body. 512 MiB over a slow link needs far more than
    #: the previous 300 s; the proxy read timeout must not be shorter than this.
    upload_timeout_seconds: int = Field(default=1800, ge=10, le=3600)
    intent_expiry_seconds: int = Field(default=900, ge=30, le=86400)
    storage_timeout_seconds: int = Field(default=20, ge=1, le=120)
    dispatch_interval_seconds: float = Field(default=2, ge=0.1, le=60)
    delivery_retry_seconds: int = Field(default=30, ge=1, le=3600)
    dispatch_batch_size: int = Field(default=20, ge=1, le=100)

    def validation_timeout_for(self, size_bytes: int) -> float:
        """Structural-validation budget for a file of this size, bounded at both ends."""
        allowance = self.validation_seconds_per_mib * (size_bytes / (1024 * 1024))
        return float(
            min(self.validation_timeout_seconds + allowance, self.validation_timeout_max_seconds)
        )
