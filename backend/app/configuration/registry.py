"""Explicit allowlist over existing policies. The browser owns no lifecycle rules."""

from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import DomainError
from app.models.enums import SourceType
from app.sufficiency.model import ASSESSMENT_SOURCE_TYPES

Lifecycle = Literal[
    "RUNTIME_SAFE",
    "RESTART_REQUIRED",
    "REINDEX_REQUIRED",
    "RECHUNK_REINDEX_REQUIRED",
    "IMMUTABLE",
    "SECRET_MANAGED_EXTERNALLY",
]
Scope = Literal["TENANT", "SYSTEM"]


@dataclass(frozen=True)
class Entry:
    key: str
    section: str
    lifecycle: Lifecycle
    editable: bool = True
    scope: Scope = "TENANT"
    description: str = ""

    @property
    def impact(self) -> str:
        return {
            "RUNTIME_SAFE": (
                "Applies atomically to subsequent requests in this tenant. "
                "In-flight requests retain their policy snapshot."
            ),
            "RESTART_REQUIRED": (
                "Managed externally. Restart the owning services with the "
                "approved deployment configuration; this UI cannot restart "
                "services."
            ),
            "REINDEX_REQUIRED": (
                "Pending external rebuild. Create and verify a new index/version "
                "before activation. The active index and effective configuration "
                "remain unchanged."
            ),
            "RECHUNK_REINDEX_REQUIRED": (
                "Pending external rebuild. Create a new chunk run, then rebuild "
                "and verify dense and sparse indexes before activation. The "
                "active corpus remains unchanged."
            ),
            "IMMUTABLE": (
                "Read-only architectural invariant. Administration cannot weaken this requirement."
            ),
            "SECRET_MANAGED_EXTERNALLY": (
                "Credential presence only. Provision secrets outside this UI and "
                "restart the API; values are never returned or stored here."
            ),
        }[self.lifecycle]


def entries() -> dict[str, Entry]:
    result: dict[str, Entry] = {}

    def add(
        section: str,
        group: str,
        fields: str,
        lifecycle: Lifecycle = "RUNTIME_SAFE",
        editable: bool = True,
        scope: Scope = "TENANT",
        description: str = "",
    ) -> None:
        for field in fields.split():
            key = f"{group}.{field}" if group else field
            result[key] = Entry(key, section, lifecycle, editable, scope, description)

    add(
        "Retrieval",
        "retrieval",
        "dense_top_k sparse_top_k final_top_k rrf_k dense_weight sparse_weight bm25_k1 bm25_b mode",
        description=(
            "First-stage retrieval policy. RRF combines ranks; scores are "
            "diagnostics, never medical confidence. M6 and Ask continue to "
            "require hybrid retrieval."
        ),
    )
    add("Retrieval", "retrieval", "degradation_policy", "IMMUTABLE", False)
    add(
        "Retrieval",
        "sparse_analyzer",
        "case_policy compound_policy min_term_length max_term_length",
        "REINDEX_REQUIRED",
    )
    add(
        "Reranking",
        "reranking",
        "candidate_top_k final_top_k",
        description=(
            "Full-text CrossEncoder candidate pool and anchor count. Raw "
            "logits are ranking diagnostics."
        ),
    )
    add(
        "Reranking",
        "reranker",
        "batch_size torch_threads request_timeout_seconds",
        "RESTART_REQUIRED",
        False,
        "SYSTEM",
    )
    add(
        "Evidence",
        "expansion",
        (
            "parent_enabled max_parent_tokens max_expansions_per_anchor "
            "previous_siblings next_siblings max_neighbour_tokens"
        ),
    )
    add(
        "Evidence",
        "evidence_budget",
        "max_total_tokens max_blocks max_tokens_per_block",
        description=(
            "Atomic artifacts are retained whole or omitted with a finding; no silent truncation."
        ),
    )
    add(
        "Chunking",
        "chunking",
        (
            "child_target_tokens parent_target_tokens table_max_tokens "
            "explanation_max_tokens figure_neighbour_elements "
            "formula_neighbour_elements"
        ),
        "RECHUNK_REINDEX_REQUIRED",
    )
    add("Chunking", "chunking", "overlap_tokens", "IMMUTABLE", False)
    for group in ("embedding", "query_encoder", "reranker"):
        add(
            "Models",
            group,
            "model_id model_revision",
            "REINDEX_REQUIRED" if group != "reranker" else "RESTART_REQUIRED",
            False,
            "SYSTEM",
        )
    add(
        "Models",
        "embedding",
        "pooling_strategy embedding_dimension normalization distance_metric input_builder_version",
        "REINDEX_REQUIRED",
        False,
        "SYSTEM",
    )
    add(
        "Models",
        "",
        "generator verifier",
        description=(
            "Only startup-approved provider/model pairs with an installed "
            "adapter are selectable. An unavailable credential fails closed."
        ),
    )
    add("Models", "credentials", "openai anthropic", "SECRET_MANAGED_EXTERNALLY", False, "SYSTEM")
    for kind in ("ordinary", "table", "formula", "figure", "assessment"):
        add("Grounding", f"sufficiency.{kind}", "min_supporting_blocks min_independent_sources")
        add(
            "Grounding",
            f"sufficiency.{kind}",
            (
                "require_non_assessment_source require_table_structure "
                "require_formula_source require_visual_interpretation"
            ),
            "IMMUTABLE",
            False,
        )
    add(
        "Grounding",
        "sufficiency",
        (
            "retrieval_scores_permitted vision_analysis_available "
            "budget_omission_is_insufficient "
            "incomplete_context_is_insufficient conflict_policy"
        ),
        "IMMUTABLE",
        False,
    )
    add(
        "Grounding",
        "grounding",
        (
            "pretrained_knowledge_is_evidence corpus_access "
            "provider_tools_enabled provider_web_search_enabled"
        ),
        "IMMUTABLE",
        False,
    )
    add("Safety", "repair", "enabled")
    add("Safety", "repair", "max_attempts may_widen_evidence", "IMMUTABLE", False)
    add("Safety", "ask", "requires_verified_pass stream_answer_tokens", "IMMUTABLE", False)
    add(
        "Safety",
        "claim_verification",
        (
            "deterministic_checks_are_final "
            "require_citation_for_material_claims check_numeric_agreement "
            "check_negation_agreement check_certainty_overstatement "
            "semantic_verification_enabled verifier_required"
        ),
        "IMMUTABLE",
        False,
    )
    add(
        "Safety",
        "final_verification",
        (
            "require_all_material_claims_supported "
            "require_deterministic_citation_success contradiction_abstains "
            "verifier_failure_abstains visual_claims_abstain"
        ),
        "IMMUTABLE",
        False,
    )
    add(
        "Safety",
        "contradiction",
        (
            "assessment_cannot_override_reference rank_may_break_ties "
            "check_uncited_retained_evidence check_authoritative_disagreement"
        ),
        "IMMUTABLE",
        False,
    )
    add("Safety", "embedding", "truncation_policy", "IMMUTABLE", False, "SYSTEM")
    add(
        "Ingestion",
        "ingestion",
        "max_upload_bytes max_retries validation_timeout_seconds upload_timeout_seconds",
        "RESTART_REQUIRED",
        False,
        "SYSTEM",
    )
    add("Ingestion", "sparse_index", "verify_postings", "IMMUTABLE", False, "SYSTEM")
    add(
        "Observability",
        "",
        "service_name",
        "RESTART_REQUIRED",
        False,
        "SYSTEM",
        (
            "Process identity used by operational health and telemetry. "
            "Audit/security events are mandatory; no raw prompt logging "
            "switch is exposed."
        ),
    )
    for source in SourceType:
        add(
            "Source Authority",
            "authority",
            source.value,
            "IMMUTABLE",
            False,
            description=(
                "Existing source classification. Assessment sources cannot "
                "override reference sources; document authority changes use the "
                "audited document metadata workflow."
            ),
        )
    return result


REGISTRY = entries()


def model_choices(settings: Settings) -> dict[str, dict[str, str]]:
    choices = {}
    for model in (*settings.approved_models, settings.generator, settings.verifier):
        if model is not None:
            choices[f"{model.provider}:{model.model_id}"] = model.model_dump()
    return choices


def value(settings: Settings, key: str) -> Any:
    if key.startswith("credentials."):
        return bool(getattr(settings, key.split(".")[1] + "_api_key").get_secret_value())
    if key.startswith("authority."):
        return "ASSESSMENT" if key.split(".")[1] in ASSESSMENT_SOURCE_TYPES else "DOCUMENT_METADATA"
    obj: Any = settings
    for part in key.split("."):
        obj = getattr(obj, part)
    if key in {"generator", "verifier"}:
        return f"{obj.provider}:{obj.model_id}" if obj else None
    return obj


def public_snapshot(settings: Settings) -> dict[str, Any]:
    return {key: value(settings, key) for key in REGISTRY}


def overlay(settings: Settings, values: dict[str, Any]) -> Settings:
    updates: dict[str, Any] = {}
    for key, new in values.items():
        entry = REGISTRY.get(key)
        if entry is None or not entry.editable:
            raise DomainError("SETTING_READ_ONLY", "The setting cannot be changed here.", 422)
        group, *parts = key.split(".")
        if not parts:
            choices = model_choices(settings)
            if new is not None and (not isinstance(new, str) or new not in choices):
                raise DomainError("MODEL_NOT_APPROVED", "Select a backend-approved model.", 422)
            updates[group] = None if new is None else choices[new]
            continue
        if group not in updates:
            updates[group] = getattr(settings, group).model_dump(mode="python")
        target = updates[group]
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = new
    try:
        # Validate the existing models in full, including cross-field bounds. Never use an
        # unchecked model_copy to introduce browser values into a frozen policy.
        from app.core.config import ModelSelection

        validated = {
            group: (ModelSelection.model_validate(data, strict=True) if data is not None else None)
            if group in {"generator", "verifier"}
            else type(getattr(settings, group)).model_validate(data, strict=True)
            for group, data in updates.items()
        }
        candidate = settings.model_copy(update=validated)
        # Only invariants the underlying policies already declare. M10 is a configuration
        # surface over verified policy; it must not invent constraints M5-M8 do not hold.
        #
        # M5 RetrievalConfig rejects final_top_k > dense_top_k + sparse_top_k, and M6 assembles
        # its pooling config by assigning candidate_top_k into that field through model_copy,
        # which does not re-run validators. Checking it here is what keeps that state
        # unconstructable rather than silently invalid.
        if (
            candidate.reranking.candidate_top_k
            > candidate.retrieval.dense_top_k + candidate.retrieval.sparse_top_k
        ):
            raise ValueError("Candidate pool exceeds lane budget")
        # M5 rejects two zero weights only under HYBRID_RRF, but M6 and Ask always search with
        # mode="HYBRID_RRF" regardless of the configured mode, so both lanes at zero disables
        # retrieval on the path this surface configures.
        if candidate.retrieval.dense_weight == candidate.retrieval.sparse_weight == 0:
            raise ValueError("Both lanes disabled")
        # No relation is asserted between max_total_tokens and max_tokens_per_block. M6 applies
        # them independently in assembly -- a block is refused if it exceeds either -- so a total
        # smaller than the per-block ceiling is a valid, tested M6 state meaning the total binds
        # first. Rejecting it here would let M10 redefine M6 policy semantics.
        return candidate
    except (ValidationError, ValueError, TypeError):
        raise DomainError(
            "SETTING_VALIDATION_FAILED",
            "Values violate typed bounds or related policy limits. No changes were applied.",
            422,
        ) from None


def metadata(settings: Settings, key: str) -> dict[str, Any]:
    if key in {"generator", "verifier"}:
        return {
            "value_type": "string",
            "allowed_values": [None, *model_choices(settings)],
            "bounds": {},
        }
    current = value(settings, key)
    schema: dict[str, Any] = {}
    obj: Any = settings
    if not key.startswith(("authority.", "credentials.")):
        for part in key.split("."):
            if isinstance(obj, BaseModel):
                field = type(obj).model_fields[part]
                schema = (
                    {"enum": list(get_args(field.annotation))}
                    if get_origin(field.annotation) is Literal
                    else {}
                )
                for constraint in field.metadata:
                    for attr, name in (
                        ("ge", "minimum"),
                        ("le", "maximum"),
                        ("gt", "exclusiveMinimum"),
                        ("lt", "exclusiveMaximum"),
                    ):
                        bound = getattr(constraint, attr, None)
                        if bound is not None:
                            schema[name] = bound
            obj = getattr(obj, part)
    return {
        "value_type": "boolean"
        if isinstance(current, bool)
        else "integer"
        if isinstance(current, int)
        else "number"
        if isinstance(current, float)
        else "string",
        "allowed_values": schema.get("enum", []),
        "bounds": {
            k: schema[k]
            for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
            if k in schema
        },
    }
