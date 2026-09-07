from dataclasses import dataclass
from hmac import compare_digest
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.core.errors import DomainError

#: The complete capability inventory. Before M12 this set omitted `audit:read`, `settings:read`
#: and `settings:write` even though routes enforced them, so it was not usable as the source of
#: truth an RBAC review needs. Every capability any route enforces now appears here, and
#: `test_m12_units.py` fails if a route enforces one that does not.
PERMISSIONS = frozenset(
    {
        # --- Library
        "document:read",
        "document:upload",
        "document:manage",
        # --- Ingestion operation. These are the operate-the-pipeline capabilities; §5 of the M12
        # brief calls this shape "operations:admin", and rather than duplicate it under a second
        # name the existing scopes keep theirs and the mapping is documented in security.md.
        "ingestion:read",
        "ingestion:retry",
        "ingestion:reparse",
        "ingestion:rechunk",
        "ingestion:reembed",
        "ingestion:reindex",
        "ingestion:cancel",
        # --- Diagnostics. Retrieval exposes lane scores and internal run identifiers and is not an
        # answering path; drafts are unverified; verifier verdicts are internal. Each stays
        # separate from the reading capability so a reader never acquires an inspector's view.
        "retrieval:search",
        "generation:draft",
        "generation:verify",
        # --- Reading. Asking is the reading capability (M9).
        "ask:submit",
        "conversation:read",
        # --- Administration
        "settings:read",
        "settings:write",
        "audit:read",
        # --- Operations (M12). Read-only operational state: queue depth, job counts, model and
        # index readiness, migration head. Separate from `settings:write` because watching the
        # system is not the same authority as changing it.
        "operations:read",
    }
)

#: Roles as explicit subsets. Previously `curator` was defined as "all of PERMISSIONS", which
#: meant that adding any capability to the inventory silently granted it to curators — including
#: the administrative ones. Enumerating each role makes a widening deliberate.
_READER = frozenset({"document:read", "ingestion:read", "ask:submit", "conversation:read"})
_CURATOR = _READER | frozenset(
    {
        "document:upload",
        "document:manage",
        "ingestion:retry",
        "ingestion:reparse",
        "ingestion:rechunk",
        "ingestion:reembed",
        "ingestion:reindex",
        "ingestion:cancel",
        "retrieval:search",
        "generation:draft",
        "generation:verify",
    }
)
_ADMIN = _CURATOR | frozenset({"settings:read", "settings:write", "audit:read", "operations:read"})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reader": _READER,
    "curator": _CURATOR,
    "admin": _ADMIN,
}

ROLES = tuple(ROLE_PERMISSIONS)


class DevCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    token: SecretStr = Field(min_length=32)
    user_id: UUID
    tenant_id: UUID
    display_name: str = Field(min_length=1, max_length=120)
    role: Literal["reader", "curator", "admin"]


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    tenant_id: UUID
    display_name: str
    role: str

    @property
    def permissions(self) -> frozenset[str]:
        """An unrecognised role grants nothing.

        Previously this indexed the mapping directly, so a role the deployment had not defined
        raised `KeyError` and surfaced as a 500 rather than a denial. Failing closed to the empty
        set makes an unmapped identity harmless instead of an internal error, and keeps the
        outcome the same whether the mapping is wrong or the role is genuinely unprivileged.
        """
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise DomainError("FORBIDDEN", "You do not have permission for this action.", 403)

    def require_any(self, *permissions: str) -> None:
        """Allow a stage to run for any principal entitled to reach it.

        Retrieval, drafting and verification each run for two kinds of caller: a reviewer using the
        diagnostic endpoints, and an ordinary reader asking a question. Each route still enforces
        its own scope, so a reader is refused at `/retrieval/draft` and admitted at `/ask` — but the
        shared stages in between must not refuse the reader whose question they are answering.
        """
        if not set(permissions) & self.permissions:
            raise DomainError("FORBIDDEN", "You do not have permission for this action.", 403)


class AuthProvider(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


class DevAuthProvider:
    """Local bearer-key adapter. No user/role/tenant is accepted from request payloads."""

    def __init__(self, credentials: tuple[DevCredential, ...]) -> None:
        self.credentials = credentials

    def authenticate(self, authorization: str | None) -> Principal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 1024:
            raise DomainError("UNAUTHORIZED", "A valid development access key is required.", 401)
        for item in self.credentials:
            if compare_digest(token.encode(), item.token.get_secret_value().encode()):
                return Principal(item.user_id, item.tenant_id, item.display_name, item.role)
        raise DomainError("UNAUTHORIZED", "A valid development access key is required.", 401)


def build_auth_provider(settings: object) -> AuthProvider:
    """Choose the identity adapter from configuration, never from a request.

    The development adapter is reachable only in development or test mode. `Settings` already
    refuses to construct a production configuration that selects it, so this is the second of two
    independent barriers rather than the only one — the first is a typed validator that cannot be
    reached around, and this one catches a `Settings` built by a test helper that bypassed it.
    """
    auth = settings.auth  # type: ignore[attr-defined]
    environment = settings.environment  # type: ignore[attr-defined]
    if auth.mode == "oidc":
        from app.security.oidc import OIDCAuthProvider

        return OIDCAuthProvider(auth)
    if environment == "production":
        raise DomainError(
            "AUTH_NOT_PRODUCTION_SAFE",
            "Development authentication cannot be used in production.",
            500,
        )
    return DevAuthProvider(settings.dev_principals)  # type: ignore[attr-defined]
