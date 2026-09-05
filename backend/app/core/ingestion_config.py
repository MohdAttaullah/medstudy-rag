from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IngestionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = Field(default="ingestion-m1-v1", min_length=1, max_length=80)
    max_upload_bytes: int = Field(default=128 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    allowed_mime_types: tuple[Literal["application/pdf"], ...] = ("application/pdf",)
    duplicate_policy: Literal["reject-within-tenant"] = "reject-within-tenant"
    max_retries: int = Field(default=3, ge=0, le=10)
    stream_chunk_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)
    validation_timeout_seconds: int = Field(default=20, ge=1, le=120)
    upload_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    intent_expiry_seconds: int = Field(default=900, ge=30, le=86400)
    storage_timeout_seconds: int = Field(default=20, ge=1, le=120)
    dispatch_interval_seconds: float = Field(default=2, ge=0.1, le=60)
    delivery_retry_seconds: int = Field(default=30, ge=1, le=3600)
    dispatch_batch_size: int = Field(default=20, ge=1, le=100)
