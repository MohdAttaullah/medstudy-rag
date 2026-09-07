from dataclasses import dataclass
from hmac import compare_digest
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.core.errors import DomainError

PERMISSIONS = frozenset(
    {
        "document:read",
        "document:upload",
        "document:manage",
        "ingestion:read",
        "ingestion:retry",
        "ingestion:reparse",
        "ingestion:rechunk",
        "ingestion:reembed",
        "ingestion:reindex",
        "ingestion:cancel",
        # Retrieval is a developer and operator tool at this milestone, not a reading feature:
        # it exposes lane scores and internal run identifiers, and it is not an answering path.
        "retrieval:search",
        # M7 drafts are unverified and expose the sufficiency decision, so they stay an authorized
        # inspection capability rather than a reading feature. The final Ask experience is M9.
        "generation:draft",
        # M8 verification can release an answer, so it is a separate capability from producing an
        # unverified draft. The user-facing Ask experience is still M9 and still disabled.
        "generation:verify",
        # M9. Asking is the reading capability; the diagnostic scopes above stay separate so a
        # reader who may ask a question does not thereby gain the inspector's view of drafts,
        # lane scores and verifier verdicts.
        "ask:submit",
        "conversation:read",
    }
)
ROLE_PERMISSIONS = {
    # A reader may ask and read their own conversations, and still may not see an unverified
    # draft, a lane score or a verifier verdict.
    "reader": frozenset({"document:read", "ingestion:read", "ask:submit", "conversation:read"}),
    "curator": PERMISSIONS,
    "admin": PERMISSIONS | {"audit:read", "settings:read", "settings:write"},
}


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
        return ROLE_PERMISSIONS[self.role]

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
