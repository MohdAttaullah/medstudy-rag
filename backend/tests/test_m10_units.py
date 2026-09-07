"""M10 allowlist, lifecycle, validation and non-negotiable safety boundaries."""

import json

import pytest
from app.configuration.registry import REGISTRY, metadata, model_choices, overlay, public_snapshot
from app.core.config import ModelSelection, Settings
from app.core.errors import DomainError
from app.core.reranking_config import EvidenceBudgetConfig
from app.core.retrieval_config import RetrievalConfig
from app.schemas.configuration import PreviewRequest
from pydantic import ValidationError


@pytest.mark.parametrize(
    "key,lifecycle",
    [
        ("retrieval.dense_top_k", "RUNTIME_SAFE"),
        ("retrieval.rrf_k", "RUNTIME_SAFE"),
        ("embedding.model_id", "REINDEX_REQUIRED"),
        ("sparse_analyzer.case_policy", "REINDEX_REQUIRED"),
        ("chunking.child_target_tokens", "RECHUNK_REINDEX_REQUIRED"),
        ("reranker.batch_size", "RESTART_REQUIRED"),
        ("credentials.openai", "SECRET_MANAGED_EXTERNALLY"),
        ("ask.requires_verified_pass", "IMMUTABLE"),
        ("repair.max_attempts", "IMMUTABLE"),
    ],
)
def test_actual_lifecycle(key, lifecycle):
    assert REGISTRY[key].lifecycle == lifecycle


def test_all_sections_are_real_and_every_entry_resolves():
    settings = Settings.model_construct()
    assert {e.section for e in REGISTRY.values()} == {
        "Chunking",
        "Retrieval",
        "Models",
        "Reranking",
        "Evidence",
        "Grounding",
        "Source Authority",
        "Ingestion",
        "Safety",
        "Observability",
    }
    snapshot = public_snapshot(settings)
    for key, entry in REGISTRY.items():
        assert key in snapshot and entry.impact
        assert metadata(settings, key)["value_type"] in {"integer", "number", "boolean", "string"}
    assert "database_url" not in snapshot and "openai_api_key" not in snapshot


@pytest.mark.parametrize(
    "key,value",
    [
        ("retrieval.dense_top_k", 0),
        ("retrieval.dense_top_k", True),
        ("retrieval.dense_top_k", "40"),
        ("retrieval.dense_weight", float("nan")),
        ("retrieval.sparse_weight", float("inf")),
        ("retrieval.mode", "UNKNOWN"),
        ("evidence_budget.max_total_tokens", -1),
        ("reranking.final_top_k", 30),
        ("generator", "unknown:model"),
        ("generator", "openai:not-approved"),
        ("repair.max_attempts", 10),
        ("ask.requires_verified_pass", False),
        ("claim_verification.semantic_verification_enabled", False),
        ("sufficiency.conflict_policy", "REPORT_ONLY"),
        ("database_url", "secret"),
        ("openai_api_key", "secret"),
    ],
)
def test_invalid_and_unsafe_values_are_rejected(key, value):
    with pytest.raises(DomainError):
        overlay(Settings.model_construct(), {key: value})


def test_cross_field_bounds_apply_atomically():
    original = Settings.model_construct()
    with pytest.raises(DomainError):
        overlay(original, {"retrieval.dense_weight": 0, "retrieval.sparse_weight": 0})
    with pytest.raises(DomainError):
        overlay(
            original, {"chunking.child_target_tokens": 2000, "chunking.parent_target_tokens": 500}
        )
    changed = overlay(original, {"retrieval.dense_top_k": 50, "reranking.final_top_k": 3})
    assert changed.retrieval.dense_top_k == 50 and original.retrieval.dense_top_k == 40
    assert changed.retrieval.fingerprint != original.retrieval.fingerprint
    assert (
        changed.embedding == original.embedding
        and changed.sparse_analyzer == original.sparse_analyzer
    )


def test_m10_does_not_add_constraints_m6_does_not_hold():
    """A configuration surface must reflect real policy semantics, not tidier ones.

    M6 applies the two evidence caps independently in assembly -- a block is refused if it
    exceeds either -- so a total smaller than the per-block ceiling simply means the total
    binds first. test_m6_units.py builds exactly that state (max_total_tokens=4 against the
    default 1024 per block) and asserts the resulting single block and finding. M10 rejecting
    it would silently redefine verified M6 policy.
    """
    original = Settings.model_construct()
    assert original.evidence_budget.max_tokens_per_block == 1024

    changed = overlay(original, {"evidence_budget.max_total_tokens": 64})
    assert changed.evidence_budget.max_total_tokens == 64
    assert changed.evidence_budget.max_tokens_per_block == 1024

    # The state the M6 suite itself constructs stays constructable through M10.
    EvidenceBudgetConfig(max_total_tokens=4)
    assert overlay(original, {"evidence_budget.max_tokens_per_block": 4096}) is not None


def test_constraints_m5_does_declare_are_still_enforced():
    """The two cross-field rules M10 keeps are M5's own, not M10 inventions.

    M5 rejects final_top_k > dense_top_k + sparse_top_k, and M6 assigns candidate_top_k into
    that field with model_copy, which skips validators -- so without this check the invalid
    state is reachable through configuration. Both lanes at zero disables retrieval because
    M6 and Ask always search with mode="HYBRID_RRF".
    """
    original = Settings.model_construct()
    with pytest.raises(ValidationError):
        RetrievalConfig(dense_top_k=10, sparse_top_k=10, final_top_k=40)
    with pytest.raises(DomainError):
        overlay(original, {"retrieval.dense_top_k": 5, "retrieval.sparse_top_k": 5})
    with pytest.raises(DomainError):
        overlay(original, {"retrieval.dense_weight": 0, "retrieval.sparse_weight": 0})


def test_provider_registry_is_backend_owned_and_optional():
    settings = Settings.model_construct(
        approved_models=(ModelSelection(provider="openai", model_id="approved-a"),)
    )
    assert list(model_choices(settings)) == ["openai:approved-a"]
    assert overlay(settings, {"generator": "openai:approved-a"}).generator.model_id == "approved-a"
    assert overlay(settings, {"generator": None}).generator is None
    assert model_choices(Settings.model_construct()) == {}


@pytest.mark.parametrize(
    "extra", [{"tenant_id": "foreign"}, {"scope": "SYSTEM"}, {"lifecycle": "RUNTIME_SAFE"}]
)
def test_client_cannot_choose_scope_or_lifecycle(extra):
    with pytest.raises(ValidationError):
        PreviewRequest.model_validate(
            {"expected_revision": 0, "changes": [{"key": "retrieval.rrf_k", "value": 50}], **extra}
        )


def test_no_nonfinite_json_values():
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValidationError):
            PreviewRequest.model_validate_json(
                json.dumps(
                    {
                        "expected_revision": 0,
                        "changes": [{"key": "retrieval.dense_weight", "value": value}],
                    }
                )
            )
