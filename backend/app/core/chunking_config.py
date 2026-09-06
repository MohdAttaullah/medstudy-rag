"""Versioned M3 policy. All fields participate in the immutable fingerprint."""

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChunkThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tiny_tokens: int = Field(default=24, ge=0, le=100)
    max_tiny_ratio: float = Field(default=0.7, ge=0, le=1)
    max_oversized_ratio: float = Field(default=0.05, ge=0, le=1)
    max_atomic_tokens: int = Field(default=2048, ge=128, le=16000)


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = Field(default="chunking-m3-v1", min_length=1, max_length=80)
    chunker_name: Literal["medical-structure"] = "medical-structure"
    chunker_version: Literal["1.0.0"] = "1.0.0"
    tokenizer_name: Literal["ncbi/MedCPT-Article-Encoder"] = "ncbi/MedCPT-Article-Encoder"
    tokenizer_revision: Literal["d05a736da4bb84ee4057b7f7999485be6ed85465"] = (
        "d05a736da4bb84ee4057b7f7999485be6ed85465"
    )
    tokenizer_sha256: Literal[
        "6e046044df8a2fcedb10607075dca187cae61d806c0d80a96c5b81017edc90c9"
    ] = "6e046044df8a2fcedb10607075dca187cae61d806c0d80a96c5b81017edc90c9"
    tokenizer_runtime: Literal["0.23.2"] = "0.23.2"
    child_target_tokens: int = Field(default=384, ge=32, le=2048)
    parent_target_tokens: int = Field(default=1280, ge=128, le=8000)
    table_max_tokens: int = Field(default=450, ge=32, le=2048)
    explanation_max_tokens: int = Field(default=384, ge=32, le=2048)
    overlap_tokens: Literal[0] = 0
    include_hierarchy_context: bool = True
    include_repeated_margins: bool = False
    repeated_margin_min_pages: int = Field(default=2, ge=2, le=20)
    margin_fraction: float = Field(default=0.08, ge=0, le=0.2)
    margin_max_characters: int = Field(default=160, ge=1, le=500)
    formula_neighbour_elements: int = Field(default=1, ge=0, le=2)
    figure_neighbour_elements: int = Field(default=1, ge=0, le=2)
    max_source_elements: int = Field(default=100000, ge=1, le=1000000)
    max_source_characters: int = Field(default=20000000, ge=1000, le=100000000)
    receipt_recovery_seconds: int = Field(default=30, ge=5, le=3600)
    timeout_seconds: int = Field(default=300, ge=10, le=3600)
    lease_seconds: int = Field(default=360, ge=30, le=7200)
    thresholds: ChunkThresholds = ChunkThresholds()

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.parent_target_tokens < self.child_target_tokens:
            raise ValueError("Parent target must be at least child target")
        if self.lease_seconds <= self.timeout_seconds:
            raise ValueError("Lease must exceed the chunking timeout")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
