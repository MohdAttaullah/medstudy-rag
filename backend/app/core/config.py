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
from app.core.auth_config import AuthConfig, LimitsConfig
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


#: Credentials that ship in this repository's development compose file. Production must not use
#: them, and naming them here is safe: they are already public in `compose.yaml`.
_DEVELOPMENT_CREDENTIALS = frozenset({"minioadmin", "medrag", "medrag-dev", "changeme", "password"})


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
    auth: AuthConfig = AuthConfig()
    limits: LimitsConfig = LimitsConfig()
    grounding: GroundingConfig = GroundingConfig()
    provider: ProviderConfig = ProviderConfig()
    dev_principals: tuple[DevCredential, ...] = ()
    # Operator-controlled allowlist; never accepted from the settings API.
    approved_models: tuple[ModelSelection, ...] = ()
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
    def chunking_fits_the_encoder(self) -> Self:
        """A retrieval-eligible chunk must be embeddable by construction.

        The encoder's input is the context field and the chunk body together, under its own
        512-token limit, and it refuses to truncate. Without this check a chunking policy can be
        configured to emit bodies that no valid input can carry, and the contradiction surfaces
        only when a real document happens to contain a large enough table — two stages later, as
        an opaque embedding failure. Checked at startup, for the same reason the encoders' vector
        spaces are.

        TEXT_PARENT is deliberately absent: a parent is a context container that is never
        embedded, so its much larger target is not bound by this contract.
        """
        budget = self.chunking.retrieval_budget_tokens
        reserve = self.embedding.context_token_reserve
        if budget + reserve > self.embedding.max_input_tokens:
            raise ValueError(
                "Chunking targets cannot fit the embedding input: "
                f"{budget} chunk tokens + {reserve} reserved for context exceed the encoder's "
                f"{self.embedding.max_input_tokens}-token limit"
            )
        return self

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
    def production_is_hardened(self) -> Self:
        """Production must satisfy every control M12 established, or must not start.

        Until M12 this validator refused `production` outright, because none of these controls
        existed. It now enumerates them instead. Each rule is here rather than in a startup script
        because a startup script can be bypassed by importing the app directly, and a warning can
        be ignored; a refusal to construct `Settings` cannot be either.

        Every failure is a configuration mistake an operator can fix, so each message names the
        setting. None of them contains a secret value.
        """
        if self.environment != "production":
            return self
        problems: list[str] = []

        # --- Identity. The single most damaging mistake this system can make.
        if self.auth.mode != "oidc":
            problems.append(
                "MEDRAG_AUTH__MODE must be 'oidc' in production; the development bearer adapter "
                "has no expiry, no revocation and no issuer"
            )
        if self.dev_principals:
            problems.append(
                "MEDRAG_DEV_PRINCIPALS must be empty in production; development identities are "
                "static tokens that never expire"
            )

        # --- Model provisioning. `offline` defaults false so a developer can provision weights,
        # and compose sets it true for the services that load them. A production deployment that
        # forgot the variable would silently fetch a model revision at query time, which is the
        # supply-chain hazard M12 must close. Requiring it here makes the permissive default
        # unreachable in production rather than relying on every deployment remembering.
        if not self.embedding.offline:
            problems.append("MEDRAG_EMBEDDING__OFFLINE must be true in production")
        if not self.query_encoder.offline:
            problems.append("MEDRAG_QUERY_ENCODER__OFFLINE must be true in production")

        # --- Abuse ceilings. Off by default so local work is unobstructed; mandatory here.
        if not self.limits.rate_limiting_enabled:
            problems.append(
                "MEDRAG_LIMITS__RATE_LIMITING_ENABLED must be true in production; Ask can spend "
                "two provider calls per request"
            )

        # --- Transport and exposure.
        if any(origin.strip() == "*" for origin in self.cors_origins):
            problems.append(
                "MEDRAG_CORS_ORIGINS must not contain '*' in production; this API is credentialed"
            )
        if not self.cors_origins:
            problems.append("MEDRAG_CORS_ORIGINS must list the browser origins allowed to call it")
        insecure = [o for o in self.cors_origins if o.strip().startswith("http://")]
        if insecure:
            problems.append(f"production CORS origins must be HTTPS: {', '.join(insecure)}")

        # --- Credentials must exist and must not be the development defaults.
        for name, value in (
            ("MEDRAG_DATABASE_URL", self.database_url.get_secret_value()),
            ("MEDRAG_REDIS_URL", self.redis_url.get_secret_value()),
            ("MEDRAG_S3_ACCESS_KEY", self.s3_access_key.get_secret_value()),
            ("MEDRAG_S3_SECRET_KEY", self.s3_secret_key.get_secret_value()),
        ):
            if not value.strip():
                problems.append(f"{name} is required in production")
        if self.s3_access_key.get_secret_value() in _DEVELOPMENT_CREDENTIALS:
            problems.append("MEDRAG_S3_ACCESS_KEY is a known development default")
        if self.s3_secret_key.get_secret_value() in _DEVELOPMENT_CREDENTIALS:
            problems.append("MEDRAG_S3_SECRET_KEY is a known development default")

        # --- Backing services must not be reachable as localhost from a production API: that
        # almost always means a sidecar-less container talking to itself, not a private network.
        for name, url in (
            ("MEDRAG_QDRANT_URL", self.qdrant_url),
            ("MEDRAG_S3_ENDPOINT", self.s3_endpoint),
        ):
            if any(host in url for host in ("127.0.0.1", "localhost", "0.0.0.0")):
                problems.append(f"{name} points at localhost, which is not a production endpoint")

        # --- A configured generator without its credential fails closed at request time. Better
        # to refuse at startup, where it is one operator's problem rather than every reader's.
        if self.generator is not None and not self.credential_for(self.generator.provider):
            problems.append(
                f"generator provider '{self.generator.provider}' is configured but its API key "
                "is absent"
            )
        if self.verifier is not None and not self.credential_for(self.verifier.provider):
            problems.append(
                f"verifier provider '{self.verifier.provider}' is configured but its API key "
                "is absent"
            )

        if problems:
            listed = "".join(f"\n  - {problem}" for problem in problems)
            raise ValueError(f"Production configuration is not hardened:{listed}")
        return self

    def credential_for(self, provider: str) -> bool:
        """Whether a provider's credential is present. Presence only; never the value."""
        keys = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }
        secret = keys.get(provider.lower())
        return bool(secret and secret.get_secret_value().strip())
