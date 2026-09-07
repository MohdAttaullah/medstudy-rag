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
    with pytest.raises(ValidationError, match="development-only"):
        Settings(environment="production")
