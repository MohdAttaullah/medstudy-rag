"""Bounded, extra-forbidden contracts. Values are validated against the registered policy type."""

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

from app.configuration.registry import Lifecycle, Scope

Value = StrictBool | StrictInt | StrictFloat | StrictStr | None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SettingChange(Contract):
    key: str = Field(min_length=1, max_length=120)
    value: Value

    @field_validator("value")
    @classmethod
    def bounded_string(cls, value: Value) -> Value:
        if isinstance(value, str) and len(value) > 200:
            raise ValueError("Setting value too long")
        return value


class PreviewRequest(Contract):
    expected_revision: int = Field(ge=0)
    changes: list[SettingChange] = Field(min_length=1, max_length=40)


class ApplyRequest(PreviewRequest):
    preview_token: str = Field(min_length=64, max_length=64)
    confirmed: bool = False
    reason: str = Field(default="", max_length=500)


class SettingView(Contract):
    key: str
    section: str
    display_name: str
    description: str
    current_value: Value
    effective_value: Value
    desired_value: Value
    default_value: Value
    value_type: str
    allowed_values: list[Value]
    bounds: dict[str, float]
    lifecycle_class: Lifecycle
    scope: Scope
    editable: bool
    impact_description: str
    requires_confirmation: bool
    status: str


class SettingsView(Contract):
    revision: int
    effective_fingerprint: str
    settings: list[SettingView]
    model_registry: list[dict[str, Any]]


class PreviewView(Contract):
    revision: int
    preview_token: str
    changes: list[dict[str, Any]]
    result: str
    requires_confirmation: bool


class RevisionView(Contract):
    revision: int
    actor_id: str
    timestamp: str
    correlation_id: str
    reason: str
    result: str
    changes: list[dict[str, Any]]
    effective_snapshot: dict[str, Value]
