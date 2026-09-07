"""The operations summary contract. Read-only, and carries no credential value."""

from pydantic import BaseModel, ConfigDict


class OperationsStatus(BaseModel):
    """What an on-call engineer needs, and nothing that would help an attacker.

    Model *identity* is here because incident review needs to attribute an answer to the exact
    model and policy that produced it. Credentials appear as presence booleans only, matching the
    M10 settings projection: the same rule everywhere means there is no surface where it is
    forgotten.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: str
    service_name: str
    auth_mode: str
    rate_limiting_enabled: bool
    job_counts: dict[str, int]
    total_retries: int
    configuration_revision: int
    configuration_fingerprint: str
    pending_rebuild_settings: list[str]
    models: dict[str, str | None]
    policy_fingerprints: dict[str, str]
    credentials_present: dict[str, bool]
