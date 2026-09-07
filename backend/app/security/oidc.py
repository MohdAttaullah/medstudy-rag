"""Production identity: OIDC bearer tokens verified against the issuer's published keys.

Vendor-neutral by construction. Entra ID, Auth0, Okta and any other OIDC-compliant provider are
configured by issuer and audience alone, and nothing here knows which one is in use. The only
provider-specific decision is which claim carries the tenant and which carries roles, and both are
configuration rather than code.

What this refuses to do matters as much as what it does:

* No token is trusted without a verified signature from the issuer's JWKS. `verify_signature`
  is never disabled, not even behind a flag, because a flag that disables it is the whole
  vulnerability.
* `alg` comes from the configured allowlist, never from the token header, so a token cannot
  nominate `none` or downgrade RS256 to HS256 and have its own body verify it.
* Tenant identity comes from the verified token, never from a request body, a header or a URL.
* A role the deployment has not mapped yields no permissions rather than a default role, so a
  misconfigured mapping fails closed instead of silently granting `reader` to everyone.
"""

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from app.core.errors import DomainError
from app.security.auth import ROLE_PERMISSIONS, Principal

#: Asymmetric algorithms only. A symmetric algorithm would require the API to hold a signing key
#: capable of minting tokens, which turns a verification service into an issuer.
SUPPORTED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "PS256"})


def _unauthorized(detail: str) -> DomainError:
    """One opaque message for every rejection.

    The reason is deliberately not returned. Distinguishing "expired" from "wrong audience" from
    "unknown key" tells an attacker which half of a forgery attempt succeeded; the specific reason
    is logged server-side under the correlation id instead.
    """
    return DomainError("UNAUTHORIZED", "A valid access token is required.", 401, {"reason": detail})


@dataclass
class JWKSCache:
    """Issuer signing keys.

    Refetched when a `kid` is unknown, which is the normal signal of key rotation, and
    rate-limited so a stream of forged tokens cannot turn this into a request amplifier
    against the identity provider.
    """

    jwks_uri: str
    ttl_seconds: float = 3600.0
    min_refresh_seconds: float = 30.0
    timeout_seconds: float = 5.0
    _keys: dict[str, Any] = field(default_factory=dict)
    _fetched_at: float = 0.0

    def _fetch(self) -> None:
        try:
            response = httpx.get(self.jwks_uri, timeout=self.timeout_seconds)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                "IDENTITY_PROVIDER_UNAVAILABLE",
                "The identity provider could not be reached.",
                503,
                {"reason": type(exc).__name__},
            ) from None
        from jwt import PyJWK

        keys = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if not kid or entry.get("kty") not in {"RSA", "EC"}:
                continue
            try:
                keys[kid] = PyJWK(entry).key
            except Exception:  # noqa: BLE001 - one malformed key must not void the whole set
                continue
        if not keys:
            raise DomainError(
                "IDENTITY_PROVIDER_UNAVAILABLE",
                "The identity provider published no usable signing keys.",
                503,
            )
        self._keys, self._fetched_at = keys, time.monotonic()

    def key_for(self, kid: str) -> Any:
        now = time.monotonic()
        if not self._keys or now - self._fetched_at > self.ttl_seconds:
            self._fetch()
        if kid not in self._keys and now - self._fetched_at > self.min_refresh_seconds:
            # An unknown kid is the normal signal of key rotation. Refetching once handles it;
            # the floor stops an attacker forcing a request to the issuer per forged token.
            self._fetch()
        key = self._keys.get(kid)
        if key is None:
            raise _unauthorized("unknown_signing_key")
        return key


class OIDCAuthProvider:
    """Verifies a bearer JWT and maps its verified claims onto a `Principal`."""

    def __init__(self, config: Any, cache: JWKSCache | None = None) -> None:
        self.config = config
        self.jwks = cache or JWKSCache(config.jwks_uri, timeout_seconds=config.jwks_timeout_seconds)

    def authenticate(self, authorization: str | None) -> Principal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise _unauthorized("missing_bearer_token")
        if len(token) > self.config.max_token_bytes:
            raise _unauthorized("token_too_large")

        import jwt

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise _unauthorized("malformed_token") from None
        kid = header.get("kid")
        if not kid:
            raise _unauthorized("missing_key_id")

        try:
            claims = jwt.decode(
                token,
                key=self.jwks.key_for(kid),
                # From configuration, never from the token header: a token must not choose the
                # algorithm used to verify it.
                algorithms=list(self.config.algorithms),
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.clock_skew_seconds,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                },
            )
        except jwt.PyJWTError as exc:
            raise _unauthorized(type(exc).__name__) from None

        return self._principal(claims)

    def _principal(self, claims: dict[str, Any]) -> Principal:
        subject = str(claims.get("sub") or "").strip()
        if not subject:
            raise _unauthorized("missing_subject")

        tenant_raw = str(claims.get(self.config.tenant_claim) or "").strip()
        if not tenant_raw:
            # Fail closed. Defaulting to a shared tenant would silently place every unmapped user
            # in one dataset, which is the cross-tenant failure this system must not have.
            raise _unauthorized("missing_tenant_claim")

        role = self._role(claims)
        return Principal(
            user_id=_stable_uuid("user", self.config.issuer, subject),
            tenant_id=_as_uuid(tenant_raw, "tenant", self.config.issuer),
            display_name=str(claims.get(self.config.name_claim) or subject)[:120],
            role=role,
        )

    def _role(self, claims: dict[str, Any]) -> str:
        raw = claims.get(self.config.roles_claim)
        presented = (
            [raw]
            if isinstance(raw, str)
            else [str(item) for item in raw]
            if isinstance(raw, list)
            else []
        )
        mapped = [
            self.config.role_mapping[value]
            for value in presented
            if value in self.config.role_mapping
        ]
        if not mapped:
            raise _unauthorized("no_mapped_role")
        # Least privilege when a token carries several roles: the narrowest wins, so adding a
        # group to a user can never quietly widen what a mapped role already allowed.
        return str(min(mapped, key=lambda role: len(ROLE_PERMISSIONS[role])))


def _as_uuid(value: str, kind: str, issuer: str) -> UUID:
    """A tenant claim that is already a UUID is used as-is; anything else is hashed stably.

    Derivation is namespaced by issuer so two identity providers cannot collide onto one tenant.
    """
    try:
        return UUID(value)
    except ValueError:
        return _stable_uuid(kind, issuer, value)


def _stable_uuid(kind: str, issuer: str, value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"medrag:{kind}:{issuer}:{value}")
