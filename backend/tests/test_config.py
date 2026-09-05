import pytest
from app.core.config import Settings, VersionedPolicy
from pydantic import ValidationError


def test_nested_environment_and_secret_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRAG_POLICY__DENSE_TOP_K", "55")
    monkeypatch.setenv("MEDRAG_DATABASE_URL", "postgresql://sensitive-credential")
    settings = Settings()
    assert settings.policy.dense_top_k == 55
    assert "sensitive-credential" not in repr(settings)
    assert settings.generator is None
    assert settings.policy.calibrated_evidence_policy is None


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
