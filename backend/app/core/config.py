"""Explicit settings injection; no process-global configuration singleton."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.ingestion_config import IngestionConfig
from app.core.parsing_config import ParsingConfig
from app.security.auth import DevCredential


class VersionedPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="bootstrap-v1", min_length=1)
    child_target_tokens: int = Field(
        default=384, ge=64, le=4096, json_schema_extra={"change_class": "reindex-required"}
    )
    parent_target_tokens: int = Field(
        default=1280, ge=128, le=16384, json_schema_extra={"change_class": "reindex-required"}
    )
    dense_top_k: int = Field(
        default=40, ge=1, le=1000, json_schema_extra={"change_class": "runtime-safe"}
    )
    sparse_top_k: int = Field(
        default=40, ge=1, le=1000, json_schema_extra={"change_class": "runtime-safe"}
    )
    final_evidence_blocks: int = Field(
        default=6, ge=1, le=32, json_schema_extra={"change_class": "runtime-safe"}
    )
    late_interaction_enabled: bool = False
    # No calibrated sufficiency threshold exists yet. Generation stays unavailable.
    calibrated_evidence_policy: str | None = None

    @model_validator(mode="after")
    def validate_sizes(self) -> Self:
        if self.child_target_tokens > self.parent_target_tokens:
            raise ValueError("Child chunk target cannot exceed parent target")
        if self.final_evidence_blocks > self.dense_top_k + self.sparse_top_k:
            raise ValueError("Evidence block count exceeds candidate budget")
        return self


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openai", "anthropic"]
    model_id: str = Field(min_length=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEDRAG_", env_nested_delimiter="__", extra="ignore", frozen=True
    )

    environment: Literal["development", "test", "production"] = "development"
    service_name: str = "medical-rag-api"
    database_url: SecretStr = SecretStr("")
    redis_url: SecretStr = SecretStr("")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr = SecretStr("")
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "medical-rag-originals"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    dependency_timeout_seconds: float = Field(default=2, gt=0, le=30)
    cors_origins: list[str] = ["http://localhost:5173"]
    policy: VersionedPolicy = VersionedPolicy()
    ingestion: IngestionConfig = IngestionConfig()
    parsing: ParsingConfig = ParsingConfig()
    dev_principals: tuple[DevCredential, ...] = ()
    generator: ModelSelection | None = None
    verifier: ModelSelection | None = None

    @model_validator(mode="after")
    def reject_unhardened_production(self) -> Self:
        if self.environment == "production":
            raise ValueError(
                "This service is development-only; production security is not implemented"
            )
        return self
