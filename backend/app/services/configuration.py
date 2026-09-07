"""Validate, preview, commit and resolve immutable tenant policies."""

import hmac
import re
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.configuration.registry import (
    REGISTRY,
    metadata,
    model_choices,
    overlay,
    public_snapshot,
    value,
)
from app.core.config import Settings
from app.core.errors import DomainError
from app.core.retrieval_config import _fingerprint
from app.models.configuration import ConfigurationRevision
from app.observability.ingestion import audit
from app.repositories.configuration import ConfigurationRepository
from app.schemas.configuration import ApplyRequest, PreviewRequest
from app.security.auth import Principal


class ConfigurationService:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings
        self.startup = public_snapshot(settings)
        self.startup_fingerprint = _fingerprint(self.startup)

    def resolve(self, actor: Principal) -> tuple[Settings, dict[str, Any]]:
        with self.sessions() as session:
            row = ConfigurationRepository(session, actor.tenant_id).latest()
            runtime = row.runtime_values if row else {}
            revision = row.revision if row else 0
            if any(
                REGISTRY.get(key) is None or REGISTRY[key].lifecycle != "RUNTIME_SAFE"
                for key in runtime
            ):
                raise DomainError(
                    "POLICY_INCOMPATIBLE",
                    "Stored policy is incompatible; current request refused.",
                    503,
                )
            effective = overlay(self.settings, runtime)
        snapshot = public_snapshot(effective)
        return effective, {
            "revision": revision,
            "effective_fingerprint": _fingerprint(snapshot),
            "values": snapshot,
            "runtime_values": runtime,
        }

    def read(self, actor: Principal) -> dict[str, Any]:
        actor.require("settings:read")
        with self.sessions() as session:
            row = ConfigurationRepository(session, actor.tenant_id).latest()
            effective = overlay(self.settings, row.runtime_values if row else {})
            desired = row.desired_values if row else {}
            revision = row.revision if row else 0
        defaults = Settings.model_construct()
        views = []
        for key, entry in REGISTRY.items():
            current = value(effective, key)
            views.append(
                {
                    "key": key,
                    "section": entry.section,
                    "display_name": key.replace(".", " / ").replace("_", " "),
                    "description": (
                        "No independent verifier selected: the generator model is used. "
                        if key == "verifier" and current is None
                        else ""
                    )
                    + (entry.description or entry.impact),
                    "current_value": current,
                    "effective_value": current,
                    "desired_value": desired.get(key, current),
                    "default_value": value(defaults, key),
                    **metadata(self.settings, key),
                    "lifecycle_class": entry.lifecycle,
                    "scope": entry.scope,
                    "editable": entry.editable and "settings:write" in actor.permissions,
                    "impact_description": entry.impact,
                    "requires_confirmation": entry.editable,
                    "status": "PENDING_REBUILD" if key in desired else "EFFECTIVE",
                }
            )
        models = [
            {
                "id": key,
                **model,
                "configured": value(self.settings, "credentials." + model["provider"]),
            }
            for key, model in model_choices(self.settings).items()
        ]
        return {
            "revision": revision,
            "effective_fingerprint": _fingerprint(public_snapshot(effective)),
            "settings": views,
            "model_registry": models,
        }

    def _prepare(
        self, actor: Principal, body: PreviewRequest, row: ConfigurationRevision | None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        actor.require("settings:write")
        revision = row.revision if row else 0
        if body.expected_revision != revision:
            raise DomainError(
                "CONFIGURATION_STALE", "Configuration changed. Reload and preview again.", 409
            )
        runtime = dict(row.runtime_values) if row else {}
        desired = dict(row.desired_values) if row else {}
        keys = [change.key for change in body.changes]
        if len(set(keys)) != len(keys):
            raise DomainError("DUPLICATE_SETTING", "Each setting may occur once per change.", 422)
        changes = []
        kinds = set()
        effective = overlay(self.settings, runtime)
        for change in body.changes:
            entry = REGISTRY.get(change.key)
            if entry is None:
                raise DomainError("UNKNOWN_SETTING", "This setting is not registered.", 422)
            if not entry.editable or entry.scope != "TENANT":
                raise DomainError(
                    "SETTING_READ_ONLY",
                    "This setting is managed outside tenant configuration.",
                    422,
                )
            active = entry.lifecycle == "RUNTIME_SAFE"
            kinds.add(active)
            target = runtime if active else desired
            changes.append(
                {
                    "key": change.key,
                    "old_value": target.get(change.key, value(effective, change.key)),
                    "new_value": change.value,
                    "lifecycle_class": entry.lifecycle,
                    "scope": entry.scope,
                    "impact_description": entry.impact,
                }
            )
            target[change.key] = change.value
        if len(kinds) != 1:
            raise DomainError(
                "MIXED_ACTIVATION", "Submit runtime and rebuild changes separately.", 422
            )
        # Validate effective and desired complete policy combinations separately.
        effective = overlay(self.settings, runtime)
        overlay(effective, desired)
        active = True in kinds
        preview = {
            "revision": revision,
            "changes": changes,
            "result": "ACTIVE" if active else "PENDING_REBUILD",
            "requires_confirmation": True,
        }
        preview["preview_token"] = _fingerprint(
            {**preview, "tenant_id": str(actor.tenant_id), "startup": self.startup_fingerprint}
        )
        return preview, runtime, desired

    def preview(self, actor: Principal, body: PreviewRequest) -> dict[str, Any]:
        actor.require("settings:write")
        with self.sessions() as session:
            preview, _, _ = self._prepare(
                actor, body, ConfigurationRepository(session, actor.tenant_id).latest()
            )
            return preview

    def apply(self, actor: Principal, body: ApplyRequest, correlation_id: UUID) -> dict[str, Any]:
        try:
            actor.require("settings:write")
            secrets = [
                self.settings.openai_api_key,
                self.settings.anthropic_api_key,
                self.settings.database_url,
                self.settings.s3_secret_key,
                self.settings.s3_access_key,
                self.settings.qdrant_api_key,
                self.settings.redis_url,
                *(c.token for c in self.settings.dev_principals),
            ]
            if re.search(r"(?:sk-|Bearer\s+)[A-Za-z0-9_-]{16,}", body.reason) or any(
                secret.get_secret_value() and secret.get_secret_value() in body.reason
                for secret in secrets
            ):
                raise DomainError(
                    "SECRET_IN_REASON", "Do not include credentials in a change reason.", 422
                )
            with self.sessions.begin() as session:
                repository = ConfigurationRepository(session, actor.tenant_id)
                repository.lock()
                old = repository.latest()
                preview, runtime, desired = self._prepare(actor, body, old)
                if not hmac.compare_digest(body.preview_token, preview["preview_token"]):
                    raise DomainError(
                        "PREVIEW_STALE", "Preview no longer matches. Preview again.", 409
                    )
                if not body.confirmed:
                    raise DomainError(
                        "CONFIRMATION_REQUIRED", "Confirm the displayed change impact.", 422
                    )
                row = ConfigurationRevision(
                    tenant_id=actor.tenant_id,
                    actor_id=actor.user_id,
                    correlation_id=correlation_id,
                    revision=preview["revision"] + 1,
                    result=preview["result"],
                    reason=body.reason,
                    changes=preview["changes"],
                    runtime_values=runtime,
                    desired_values=desired,
                    effective_snapshot=public_snapshot(overlay(self.settings, runtime)),
                    startup_fingerprint=self.startup_fingerprint,
                )
                session.add(row)
                session.flush()
                audit(
                    session,
                    actor.tenant_id,
                    actor.user_id,
                    "CONFIGURATION_" + row.result,
                    row.id,
                    correlation_id,
                    {
                        "revision": row.revision,
                        "changes": row.changes,
                        "scope": "TENANT",
                        "result": row.result,
                    },
                )
                result = self.history_view(row)
            return result
        except DomainError as exc:
            # Rejections are durable too, but arbitrary rejected values/keys must never be
            # echoed: they could contain a credential. Only declared result codes are recorded.
            with self.sessions.begin() as session:
                audit(
                    session,
                    actor.tenant_id,
                    actor.user_id,
                    "CONFIGURATION_REJECTED",
                    None,
                    correlation_id,
                    {"result": exc.code, "scope": "TENANT"},
                )
            raise

    @staticmethod
    def history_view(row: ConfigurationRevision) -> dict[str, Any]:
        return {
            "revision": row.revision,
            "actor_id": str(row.actor_id),
            "timestamp": row.created_at.isoformat(),
            "correlation_id": str(row.correlation_id),
            "reason": row.reason,
            "result": row.result,
            "changes": row.changes,
            "effective_snapshot": row.effective_snapshot,
        }

    def history(self, actor: Principal, limit: int, offset: int) -> list[dict[str, Any]]:
        actor.require("settings:read")
        with self.sessions() as session:
            return [
                self.history_view(row)
                for row in ConfigurationRepository(session, actor.tenant_id).history(limit, offset)
            ]
