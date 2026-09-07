"""Which identity adapter runs, and the rules that stop the development one running in production.

The development bearer adapter is a list of static tokens with no expiry, no revocation and no
issuer. It exists so the stack is usable on a laptop. The single most damaging configuration
mistake this system can make is carrying it into production, so the ban is expressed as a typed
validator that refuses to construct the settings object at all — not as a warning, a log line or a
runtime check that a later refactor could route around.
"""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator

from app.core.reranking_config import Policy

AuthMode = Literal["development", "oidc"]


class AuthConfig(Policy):
    """Identity configuration. Vendor-neutral: an OIDC issuer and audience, nothing more."""

    version: Literal["auth-m12-v1"] = "auth-m12-v1"
    mode: AuthMode = "development"

    issuer: str = ""
    audience: str = ""
    jwks_uri: str = ""
    #: Verification algorithms, taken from configuration so a token cannot nominate its own.
    #: Asymmetric only — a symmetric algorithm would make this service able to mint tokens.
    algorithms: tuple[str, ...] = ("RS256",)
    #: Claim names differ between providers; the code does not care which provider is in use.
    tenant_claim: str = "tid"
    roles_claim: str = "roles"
    name_claim: str = "name"
    #: Provider role or group value -> this system's role. Unmapped roles grant nothing.
    role_mapping: dict[str, Literal["reader", "curator", "admin"]] = Field(default_factory=dict)

    clock_skew_seconds: float = Field(default=60, ge=0, le=300)
    jwks_timeout_seconds: float = Field(default=5, gt=0, le=30)
    max_token_bytes: int = Field(default=8192, ge=512, le=65536)
    #: Present so a deployment can carry it; never returned by any API.
    oidc_client_secret: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def oidc_is_completely_configured(self) -> Self:
        """A half-configured OIDC mode must not start.

        Partial configuration is worse than none: an empty audience with signature verification on
        still accepts any token the issuer minted for any other application.
        """
        if self.mode != "oidc":
            return self
        missing = [
            name for name in ("issuer", "audience", "jwks_uri") if not getattr(self, name).strip()
        ]
        if missing:
            raise ValueError(f"OIDC authentication requires {', '.join(missing)}")
        if not self.jwks_uri.startswith("https://"):
            raise ValueError("The JWKS URI must be HTTPS; signing keys cannot be fetched in clear")
        if not self.issuer.startswith("https://"):
            raise ValueError("The OIDC issuer must be HTTPS")
        from app.security.oidc import SUPPORTED_ALGORITHMS

        unsupported = set(self.algorithms) - SUPPORTED_ALGORITHMS
        if unsupported or not self.algorithms:
            raise ValueError(
                "Only asymmetric verification algorithms are supported: "
                f"{sorted(SUPPORTED_ALGORITHMS)}"
            )
        if not self.role_mapping:
            raise ValueError(
                "OIDC authentication requires a role mapping; without one no token grants anything"
            )
        return self


class LimitsConfig(Policy):
    """Bounded request sizes and rates.

    Every value is a ceiling on work a single caller can cause. They are engineering bounds chosen
    to keep one client from saturating a replica, not measured capacity figures.
    """

    version: Literal["limits-m12-v1"] = "limits-m12-v1"

    #: Per principal, per window. Ask is the expensive one: it can spend two provider calls.
    ask_per_minute: int = Field(default=20, ge=1, le=1000)
    upload_per_minute: int = Field(default=10, ge=1, le=1000)
    #: Configuration mutation. Low by intent: this is an administrative action, not a workload.
    settings_write_per_minute: int = Field(default=12, ge=1, le=1000)
    diagnostics_per_minute: int = Field(default=60, ge=1, le=5000)
    rate_limit_window_seconds: float = Field(default=60, ge=1, le=3600)
    #: Off by default, and *required* on in production by `Settings.production_is_hardened`.
    #: This is an abuse ceiling for a public multi-tenant deployment, not a correctness control:
    #: throttling a developer's own stack, or a test harness driving hundreds of fixture uploads
    #: as one principal, would only obstruct legitimate work. Enabled explicitly, enforced where
    #: it matters, and never silently on where it would be mistaken for a bug.
    rate_limiting_enabled: bool = False

    #: Largest JSON body any route will accept. Uploads stream and are bounded separately by
    #: `IngestionConfig.max_upload_bytes`.
    max_json_body_bytes: int = Field(default=256 * 1024, ge=1024, le=8 * 1024 * 1024)

    #: Strict-Transport-Security is a promise the deployment must keep, so it is opt-in: sending
    #: it from a service reached over plain HTTP pins a browser to HTTPS the host does not serve.
    hsts_enabled: bool = False

    def budgets(self) -> dict[str, tuple[int, float]]:
        window = self.rate_limit_window_seconds
        return {
            "ask": (self.ask_per_minute, window),
            "upload": (self.upload_per_minute, window),
            "settings_write": (self.settings_write_per_minute, window),
            "diagnostics": (self.diagnostics_per_minute, window),
        }
