"""Explicit settings injection; no process-global configuration singleton."""

from typing import Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.ask_config import AskConfig
from app.core.chunking_config import ChunkingConfig
from app.core.embedding_config import EmbeddingConfig, IndexConfig
from app.core.generation_config import GroundingConfig, ProviderConfig, SufficiencyConfig
from app.core.ingestion_config import IngestionConfig
from app.core.parsing_config import ParsingConfig
from app.core.reranking_config import (
    EvidenceBudgetConfig,
    ExpansionConfig,
    RerankerConfig,
    RerankingConfig,
)
from app.core.retrieval_config import (
    QueryEncoderConfig,
    RetrievalConfig,
    SparseAnalyzerConfig,
    SparseIndexConfig,
)
from app.core.verification_config import (
    ClaimExtractionConfig,
    ClaimVerificationConfig,
    ContradictionConfig,
    FinalVerificationConfig,
    RepairConfig,
)
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
    chunking: ChunkingConfig = ChunkingConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    index: IndexConfig = IndexConfig()
    query_encoder: QueryEncoderConfig = QueryEncoderConfig()
    sparse_analyzer: SparseAnalyzerConfig = SparseAnalyzerConfig()
    sparse_index: SparseIndexConfig = SparseIndexConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    reranker: RerankerConfig = RerankerConfig()
    reranking: RerankingConfig = RerankingConfig()
    expansion: ExpansionConfig = ExpansionConfig()
    evidence_budget: EvidenceBudgetConfig = EvidenceBudgetConfig()
    sufficiency: SufficiencyConfig = SufficiencyConfig()
    claim_extraction: ClaimExtractionConfig = ClaimExtractionConfig()
    claim_verification: ClaimVerificationConfig = ClaimVerificationConfig()
    contradiction: ContradictionConfig = ContradictionConfig()
    repair: RepairConfig = RepairConfig()
    final_verification: FinalVerificationConfig = FinalVerificationConfig()
    ask: AskConfig = AskConfig()
    grounding: GroundingConfig = GroundingConfig()
    provider: ProviderConfig = ProviderConfig()
    dev_principals: tuple[DevCredential, ...] = ()
    generator: ModelSelection | None = None
    verifier: ModelSelection | None = None

    @field_validator("generator", "verifier", mode="before")
    @classmethod
    def blank_selection_is_unconfigured(cls, value: object) -> object:
        """An env var that exists but is empty means unconfigured, not misconfigured.

        Container orchestrators pass a declared variable through as an empty string rather than
        omitting it, so `MEDRAG_GENERATOR__PROVIDER=` arrives as `{"provider": ""}`. Failing
        validation there would stop the whole API from starting purely because no provider account
        is set up, when the correct behaviour is that generation is unavailable and every earlier
        stage still runs.
        """
        if isinstance(value, dict) and not any(str(item).strip() for item in value.values()):
            return None
        return value

    # Backend-only. Accepted under the conventional unprefixed names as well as the project's
    # MEDRAG_ prefix, and never echoed into a response, a log, a metric or the frontend bundle.
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPENAI_API_KEY", "MEDRAG_OPENAI_API_KEY"),
    )
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "MEDRAG_ANTHROPIC_API_KEY"),
    )

    @model_validator(mode="after")
    def retrieval_vector_spaces_agree(self) -> Self:
        """The query encoder and the article encoder must describe one vector space.

        Checked at startup rather than at the first query, because a mismatch is a configuration
        error that would otherwise surface as a plausible-looking but meaningless ranking.
        """
        divergent = self.query_encoder.incompatibility(self.embedding)
        if divergent:
            raise ValueError(
                f"Query and article encoders are not in the same vector space: {divergent}"
            )
        return self

    @model_validator(mode="after")
    def reject_unhardened_production(self) -> Self:
        if self.environment == "production":
            raise ValueError(
                "This service is development-only; production security is not implemented"
            )
        return self
