import pytest
from app.core.config import Settings, VersionedPolicy
from pydantic import ValidationError


def test_nested_environment_and_secret_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRAG_POLICY__DENSE_TOP_K", "55")
    monkeypatch.setenv("MEDRAG_DATABASE_URL", "postgresql://sensitive-credential")
    # A generator may or may not be configured in the surrounding environment from M7 onward, so
    # this test decides its own: it is about nested parsing and redaction, not about deployment.
    monkeypatch.delenv("MEDRAG_GENERATOR__PROVIDER", raising=False)
    monkeypatch.delenv("MEDRAG_GENERATOR__MODEL_ID", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sensitive-credential-key")
    settings = Settings()
    assert settings.policy.dense_top_k == 55
    assert "sensitive-credential" not in repr(settings)
    assert settings.generator is None
    assert settings.policy.calibrated_evidence_policy is None


def test_a_configured_generator_is_parsed_and_its_key_stays_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From M7 a provider may be configured; the key must never reach a repr, log or response."""
    monkeypatch.setenv("MEDRAG_GENERATOR__PROVIDER", "openai")
    monkeypatch.setenv("MEDRAG_GENERATOR__MODEL_ID", "configured-model")
    monkeypatch.setenv("OPENAI_API_KEY", "sensitive-credential-key")
    settings = Settings()
    assert settings.generator is not None
    assert (settings.generator.provider, settings.generator.model_id) == (
        "openai",
        "configured-model",
    )
    assert "sensitive-credential-key" not in repr(settings)
    assert "sensitive-credential-key" not in settings.model_dump_json()
    assert settings.openai_api_key.get_secret_value() == "sensitive-credential-key"


@pytest.mark.parametrize(
    "values",
    [
        {"dense_top_k": 0},
        {"child_target_tokens": 1600, "parent_target_tokens": 1000},
        {"dense_top_k": 1, "sparse_top_k": 1, "final_evidence_blocks": 6},
        {"unexpected_setting": True},
    ],
)
def test_invalid_policy_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VersionedPolicy.model_validate(values)


def test_unhardened_production_cannot_start() -> None:
    """Production still refuses to start unhardened — but for real reasons now.

    Before M12 this asserted a blanket refusal ("development-only"), because none of the
    production controls existed. M12 built them, so the refusal is now itemised: the assertion
    is narrowed to the specific rules rather than removed, and `test_m12_units.py` proves each
    one independently and proves that a fully hardened configuration does start.
    """
    with pytest.raises(ValidationError, match="not hardened") as caught:
        Settings(environment="production", dev_principals=[])
    message = str(caught.value)
    assert "MEDRAG_AUTH__MODE must be 'oidc'" in message
    assert "RATE_LIMITING_ENABLED" in message
