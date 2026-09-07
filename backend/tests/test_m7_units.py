"""M7 safety contracts: the sufficiency gate, provider isolation and citation binding."""

import asyncio
import inspect
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from app.core.config import ModelSelection, Settings
from app.core.generation_config import (
    EvidenceRequirement,
    GroundingConfig,
    ProviderConfig,
    SufficiencyConfig,
)
from app.evidence.model import ArtifactRef, EvidenceBlock, EvidenceSet, SourceSpan
from app.generation.citations.validate import bind
from app.generation.errors import RETRYABLE, GenerationError
from app.generation.grounding.model import DraftClaim, GroundedDraft, ProviderDraft
from app.generation.prompts.grounded import SYSTEM_POLICY, render_evidence
from app.generation.providers.anthropic import AnthropicProvider
from app.generation.providers.factory import build_provider
from app.generation.providers.fake import FakeProvider
from app.generation.providers.openai import OpenAIProvider
from app.sufficiency.conflicts import detect, measurements
from app.sufficiency.gate import SufficiencyGate
from app.sufficiency.model import SufficiencyDecision
from app.sufficiency.question import classify
from pydantic import SecretStr, ValidationError

ROOT = Path(__file__).resolve().parents[2]


def block(
    text="Warfarin is metabolised by CYP2C9.",
    *,
    source_type="TEXTBOOK",
    authority="REFERENCE",
    chunk_type="TEXT_CHILD",
    reason="RERANKED_ANCHOR",
    version=None,
    artifacts=(),
    question=None,
    representation="m3-source-with-structural-labels-v1",
    visual=False,
):
    element = uuid4()
    return EvidenceBlock(
        evidence_id=uuid4(),
        anchor_chunk_id=uuid4(),
        source_chunk_ids=[uuid4()],
        source_element_ids=[element],
        document_id=uuid4(),
        document_version_id=version or uuid4(),
        chunk_run_id=uuid4(),
        parse_run_id=uuid4(),
        document_title="Synthetic reference",
        source_type=source_type,
        authority_level=authority,
        chunk_type=chunk_type,
        pages=[3],
        hierarchy=[],
        source_spans=[
            SourceSpan(
                element_id=element,
                start=0,
                end=len(text),
                text=text,
                page=3,
                reading_order=1,
                role="PRIMARY",
                bbox=(None, None, None, None),
            )
        ],
        text=text,
        representation=representation,
        artifacts=list(artifacts),
        question=question,
        expansion_reason=reason,
        token_count=max(1, len(text) // 4),
        requires_visual_evidence=visual,
    )


def evidence(*blocks, warnings=()):
    return EvidenceSet(
        query_hash="0" * 64,
        retrieval_trace={},
        reranking_trace={"durations_ms": {}},
        anchors=[b.anchor_chunk_id for b in blocks],
        expansions=[],
        evidence_blocks=list(blocks),
        total_tokens=sum(b.token_count for b in blocks),
        requires_visual_evidence=any(b.requires_visual_evidence for b in blocks),
        warnings=list(warnings),
        duplicates_removed=0,
    )


def gate(**overrides):
    return SufficiencyGate(SufficiencyConfig(**overrides))


# --- The gate decides, and generation depends on that decision -------------------------------


def test_no_evidence_is_insufficient_not_an_empty_answer():
    decision = gate().evaluate("What is the dose?", evidence())
    assert decision.status == "INSUFFICIENT"
    assert decision.reason_codes == ["NO_EVIDENCE"]
    assert not decision.generation_permitted


def test_ordinary_factual_evidence_is_sufficient():
    decision = gate().evaluate("How is warfarin metabolised?", evidence(block()))
    assert decision.status == "SUFFICIENT" and decision.generation_permitted
    assert "SUPPORTED_BY_NON_ASSESSMENT_SOURCE" in decision.reason_codes
    assert decision.question_kind == "ORDINARY_FACTUAL"


def test_assessment_only_evidence_never_becomes_medical_truth():
    """A question bank records what an examiner marked, not what the corpus establishes."""
    only_key = block(
        source_type="QUESTION_BANK",
        authority="ASSESSMENT",
        chunk_type="QUESTION",
        question={"explicit_answer": "Option B", "question_text": "Which enzyme?"},
    )
    decision = gate().evaluate("Which enzyme metabolises warfarin?", evidence(only_key))
    assert decision.status == "INSUFFICIENT"
    assert "ASSESSMENT_ONLY_EVIDENCE" in decision.reason_codes
    assert "non_assessment_source" in decision.missing_requirements


def test_budget_omission_is_missing_evidence_not_absent_evidence():
    decision = gate().evaluate(
        "How is warfarin metabolised?",
        evidence(block(), warnings=["CONTEXT_BUDGET_EXCEEDED:" + str(uuid4())]),
    )
    assert decision.status == "INSUFFICIENT"
    assert "EVIDENCE_BUDGET_OMISSION" in decision.reason_codes


def test_partial_source_spans_count_as_incomplete_context():
    decision = gate().evaluate(
        "How is warfarin metabolised?", evidence(block(representation="source-spans-v1"))
    )
    assert decision.status == "INSUFFICIENT"
    assert "CONTEXT_INCOMPLETE" in decision.reason_codes


def test_expansion_context_is_not_independent_support():
    """Neighbour context supports its anchor; counting it as support would inflate the evidence."""
    decision = gate(ordinary=EvidenceRequirement(min_supporting_blocks=2)).evaluate(
        "How is warfarin metabolised?",
        evidence(block(), block(reason="NEXT_SIBLING"), block(reason="PARENT_EXPANSION")),
    )
    assert decision.status == "INSUFFICIENT"
    assert "INSUFFICIENT_SUPPORTING_BLOCKS" in decision.reason_codes
    assert [s.value for s in decision.evaluated_signals if s.name == "supporting_blocks"] == [1]


def test_independent_sources_are_counted_by_document_version():
    shared = uuid4()
    requirement = EvidenceRequirement(min_independent_sources=2)
    same = gate(ordinary=requirement).evaluate(
        "How is warfarin metabolised?", evidence(block(version=shared), block(version=shared))
    )
    assert same.status == "INSUFFICIENT"
    assert "INSUFFICIENT_INDEPENDENT_SOURCES" in same.reason_codes
    distinct = gate(ordinary=requirement).evaluate(
        "How is warfarin metabolised?",
        evidence(block(text="Warfarin uses CYP2C9."), block(text="Warfarin uses CYP2C9.")),
    )
    assert distinct.status == "SUFFICIENT"
    assert "SUPPORTED_BY_INDEPENDENT_SOURCES" in distinct.reason_codes


# --- Question-type-aware requirements ---------------------------------------------------------


def test_question_wording_decides_the_required_evidence_structure():
    assert classify("Which row of the table lists it?", []) == "TABLE_DEPENDENT"
    assert classify("Calculate the clearance using the equation", []) == "FORMULA_DEPENDENT"
    assert classify("What is shown in the figure?", []) == "FIGURE_DEPENDENT"
    assert classify("How is warfarin metabolised?", []) == "ORDINARY_FACTUAL"
    # Whole-term matching: a cue must not fire on a word that merely contains it.
    assert classify("Is this an acceptable dose?", []) == "ORDINARY_FACTUAL"


def test_structural_fallback_uses_the_retrieved_anchor_kind():
    assert classify("What does it state?", [block(chunk_type="TABLE_PART")]) == "TABLE_DEPENDENT"
    assert classify("What does it state?", [block(chunk_type="FORMULA")]) == "FORMULA_DEPENDENT"


def test_table_question_without_header_rows_abstains():
    """M6 measures a real table context gap; the generator must not reconstruct the missing rows."""
    headerless = ArtifactRef(
        artifact_id=uuid4(), kind="TABLE", source_element_id=uuid4(), href="/a", row_indexes=[2]
    )
    decision = gate().evaluate(
        "Which row of the table gives the dose?",
        evidence(block(chunk_type="TABLE_PART", artifacts=[headerless])),
    )
    assert decision.status == "INSUFFICIENT"
    assert "TABLE_STRUCTURE_INCOMPLETE" in decision.reason_codes


def test_table_question_with_headers_is_sufficient():
    complete = ArtifactRef(
        artifact_id=uuid4(),
        kind="TABLE",
        source_element_id=uuid4(),
        href="/a",
        row_indexes=[2],
        header_rows=[0],
    )
    decision = gate().evaluate(
        "Which row of the table gives the dose?",
        evidence(block(chunk_type="TABLE_PART", artifacts=[complete])),
    )
    assert decision.status == "SUFFICIENT"
    assert "REQUIRED_ARTIFACT_PRESENT" in decision.reason_codes


def test_figure_question_abstains_because_no_vision_path_is_approved():
    """A caption says a figure exists. It does not say what the figure shows."""
    captioned = block(chunk_type="FIGURE_CONTEXT", visual=True, text="Figure 2. Renal anatomy.")
    decision = gate().evaluate("What is shown in the figure?", evidence(captioned))
    assert decision.status == "INSUFFICIENT"
    assert "VISUAL_INTERPRETATION_UNAVAILABLE" in decision.reason_codes
    assert SufficiencyConfig().vision_analysis_available is False


def test_formula_question_without_a_formula_artifact_abstains():
    decision = gate().evaluate("Calculate the clearance", evidence(block()))
    assert decision.status == "INSUFFICIENT"
    assert "FORMULA_SOURCE_MISSING" in decision.reason_codes


# --- Conflict, not silent selection -----------------------------------------------------------


def test_independent_sources_disagreeing_on_a_value_conflict():
    decision = gate().evaluate(
        "What is the loading dose?",
        evidence(
            block(text="The loading dose is 5 mg daily."),
            block(text="The loading dose is 10 mg daily."),
        ),
    )
    assert decision.status == "CONFLICTING"
    assert "INDEPENDENT_SOURCE_VALUE_CONFLICT" in decision.reason_codes
    assert len(decision.conflicting_evidence_ids) == 2
    assert not decision.generation_permitted


def test_one_document_stating_several_values_is_a_range_not_a_conflict():
    version = uuid4()
    decision = gate().evaluate(
        "What is the loading dose?",
        evidence(
            block(text="The loading dose is 5 mg daily.", version=version),
            block(text="The loading dose is 10 mg daily.", version=version),
        ),
    )
    assert decision.status == "SUFFICIENT"


def test_different_labels_sharing_a_unit_do_not_conflict():
    decision = gate().evaluate(
        "What is the loading dose?",
        evidence(
            block(text="The loading dose is 5 mg daily."),
            block(text="The maintenance dose is 10 mg daily."),
        ),
    )
    assert decision.status == "SUFFICIENT"


def test_assessment_key_unsupported_by_reference_evidence_conflicts():
    key = block(
        source_type="ANSWER_KEY",
        authority="ASSESSMENT",
        chunk_type="QUESTION",
        text="Which enzyme?",
        question={"explicit_answer": "rifampicin", "question_text": "Which enzyme?"},
    )
    decision = gate().evaluate(
        "Which enzyme metabolises warfarin?",
        evidence(key, block(text="Warfarin is metabolised by CYP2C9.")),
    )
    assert decision.status == "CONFLICTING"
    assert "ASSESSMENT_KEY_UNSUPPORTED_BY_REFERENCE" in decision.reason_codes


def test_a_supported_assessment_key_is_not_a_conflict():
    key = block(
        source_type="QUESTION_BANK",
        authority="ASSESSMENT",
        chunk_type="QUESTION",
        text="Which enzyme?",
        question={"explicit_answer": "CYP2C9", "question_text": "Which enzyme?"},
    )
    decision = gate().evaluate(
        "Which enzyme metabolises warfarin?",
        evidence(key, block(text="Warfarin is metabolised by CYP2C9.")),
    )
    assert decision.status == "SUFFICIENT"


def test_measurement_extraction_keeps_labels_distinct():
    found = measurements("Loading dose 5 mg. Maintenance dose 10 mg.")
    assert len({label for label, _ in found}) == 2


def test_conflict_needs_reference_evidence_to_disagree_with():
    """With no reference source there is nothing to conflict with; that is insufficiency."""
    key = block(
        source_type="ANSWER_KEY",
        authority="ASSESSMENT",
        chunk_type="QUESTION",
        question={"explicit_answer": "rifampicin"},
    )
    assert detect([key]) == []


# --- No score may stand in for sufficiency -----------------------------------------------------


def test_no_retrieval_or_reranker_score_participates_in_the_decision():
    """The gate must not be reachable from a number M6 explicitly refused to calibrate."""
    config = SufficiencyConfig()
    assert config.retrieval_scores_permitted is False
    assert config.decision_basis == "DETERMINISTIC_SIGNALS"
    # No policy field is a numeric threshold, and none names a lane or a model score.
    names = set(SufficiencyConfig.model_fields) | set(EvidenceRequirement.model_fields)
    for forbidden in ("threshold", "logit", "rrf", "bm25", "dense", "sparse", "reranker"):
        assert not any(forbidden in name for name in names)
    assert all(
        not isinstance(value, float)
        for value in config.model_dump().values()
        if not isinstance(value, dict)
    )
    source = Path(ROOT, "backend/app/sufficiency/gate.py").read_text(encoding="utf-8")
    for forbidden in ("reranker_score", "fused_score", "dense_score", "sparse_score"):
        assert forbidden not in source


def test_the_decision_exposes_no_medical_confidence_number():
    decision = gate().evaluate("How is warfarin metabolised?", evidence(block()))
    dumped = decision.model_dump(mode="json")
    assert "confidence" not in dumped and "probability" not in dumped and "score" not in dumped
    assert set(SufficiencyDecision.model_fields) >= {
        "status",
        "reason_codes",
        "evaluated_signals",
        "supporting_evidence_ids",
        "conflicting_evidence_ids",
        "missing_requirements",
        "policy_version",
        "policy_fingerprint",
    }


def test_policy_is_fingerprinted_so_a_decision_is_reconstructable():
    a, b = (
        SufficiencyConfig(),
        SufficiencyConfig(ordinary=EvidenceRequirement(min_independent_sources=2)),
    )
    assert a.fingerprint != b.fingerprint
    assert gate().evaluate("q", evidence(block())).policy_fingerprint == a.fingerprint


# --- Grounding contract ------------------------------------------------------------------------


def test_the_system_policy_forbids_pretrained_knowledge_as_evidence():
    assert "Pretrained model knowledge is not valid evidence for this answer." in SYSTEM_POLICY
    assert "Do not describe or interpret an image" in SYSTEM_POLICY


def test_rendered_evidence_carries_identity_and_source_but_no_rank_or_score():
    """A generator shown a rank would treat the top block as the most true one."""
    rendered = render_evidence([block()], GroundingConfig())
    assert "evidence_id=" in rendered and "authority REFERENCE" in rendered
    for forbidden in ("rank", "score", "logit"):
        assert forbidden not in rendered.lower()


def test_grounding_policy_pins_the_evidence_boundary_by_type():
    config = GroundingConfig()
    assert config.pretrained_knowledge_is_evidence is False
    assert config.corpus_access == "EVIDENCE_SET_ONLY"
    assert config.provider_tools_enabled is False
    assert config.provider_web_search_enabled is False


def test_assessment_blocks_are_marked_in_the_rendered_evidence():
    rendered = render_evidence(
        [block(source_type="QUESTION_BANK", authority="ASSESSMENT", question={"a": 1})],
        GroundingConfig(),
    )
    assert "not established fact" in rendered


# --- Citation binding is a contract check, not verification ------------------------------------


def test_every_citation_must_name_a_supplied_evidence_block():
    approved = [uuid4(), uuid4()]
    draft = ProviderDraft(
        answer="Warfarin uses CYP2C9.",
        claims=[DraftClaim(text="Warfarin uses CYP2C9.", evidence_ids=[approved[0]])],
    )
    cited, uncited = bind(draft, approved)
    assert cited == [approved[0]] and uncited == [approved[1]]


def test_an_invented_evidence_id_is_rejected():
    approved = [uuid4()]
    draft = ProviderDraft(
        answer="Warfarin uses CYP2C9.",
        claims=[DraftClaim(text="Warfarin uses CYP2C9.", evidence_ids=[uuid4()])],
    )
    with pytest.raises(GenerationError, match="UNKNOWN_CITATION"):
        bind(draft, approved)


def test_a_real_id_from_another_request_is_still_unknown_here():
    """Existing somewhere is not the test; being in this EvidenceSet is."""
    other_request = uuid4()
    draft = ProviderDraft(
        answer="Text.", claims=[DraftClaim(text="Text.", evidence_ids=[other_request])]
    )
    with pytest.raises(GenerationError, match="UNKNOWN_CITATION"):
        bind(draft, [uuid4(), uuid4()])


def test_a_draft_binding_nothing_is_rejected():
    with pytest.raises(ValidationError):
        ProviderDraft(answer="Text.", claims=[DraftClaim(text="Text.", evidence_ids=[])])


def test_a_draft_is_typed_as_unverified():
    draft = GroundedDraft(
        draft_id=uuid4(),
        answer="Text.",
        claims=[DraftClaim(text="Text.", evidence_ids=[uuid4()])],
        cited_evidence_ids=[],
        uncited_evidence_ids=[],
        evidence_gap=None,
        query_hash="0" * 64,
        provider=FakeProvider(lambda q, e: None).specification,
        grounding_policy_version="grounding-m7-v1",
        grounding_policy_fingerprint="x",
        sufficiency_policy_fingerprint="y",
        durations_ms={},
    )
    assert draft.status == "GROUNDED_DRAFT"
    assert draft.verification_status == "UNVERIFIED_AWAITING_CLAIM_VERIFICATION"
    with pytest.raises(ValidationError):
        draft.model_copy(update={"verification_status": "VERIFIED"}).model_validate(
            {**draft.model_dump(mode="json"), "verification_status": "VERIFIED"}
        )


# --- Provider isolation and failure behaviour --------------------------------------------------


def test_no_vendor_sdk_or_endpoint_leaks_outside_the_adapters():
    """ADR-005's boundary is the reason the rest of the system stays provider-independent."""
    offenders = []
    for path in Path(ROOT, "backend/app").rglob("*.py"):
        if "generation/providers" in path.as_posix():
            continue
        body = path.read_text(encoding="utf-8").lower()
        if "api.openai.com" in body or "api.anthropic.com" in body:
            offenders.append(path.as_posix())
        if "import openai" in body or "import anthropic" in body:
            offenders.append(path.as_posix())
    assert offenders == []


def test_no_provider_key_reaches_the_frontend_bundle():
    for path in Path(ROOT, "frontend/src").rglob("*.ts*"):
        body = path.read_text(encoding="utf-8")
        assert "VITE_OPENAI" not in body and "VITE_ANTHROPIC" not in body
        assert "sk-" not in body


def test_an_unconfigured_generator_is_unavailable_not_a_default_model():
    with pytest.raises(GenerationError, match="UNCONFIGURED"):
        build_provider(Settings(generator=None))
    with pytest.raises(GenerationError, match="UNCONFIGURED"):
        build_provider(
            Settings(
                generator=ModelSelection(provider="openai", model_id="gpt-x"),
                openai_api_key=SecretStr(""),
            )
        )


def test_a_declared_but_empty_generator_variable_leaves_generation_unconfigured():
    """Compose passes a declared variable through as "", which must not stop the API booting.

    Regression: this shape crashed the API container at startup, because pydantic built
    ModelSelection from two empty strings instead of treating the selection as absent. Generation
    being unavailable must never take the rest of the pipeline down with it.
    """
    assert Settings(generator={"provider": "", "model_id": ""}).generator is None
    assert Settings(verifier={"provider": "", "model_id": ""}).verifier is None
    configured = Settings(generator={"provider": "openai", "model_id": "gpt-x"})
    assert configured.generator is not None and configured.generator.model_id == "gpt-x"
    with pytest.raises(ValidationError):
        Settings(generator={"provider": "not-a-provider", "model_id": "gpt-x"})


def test_provider_selection_is_configuration_driven():
    openai = build_provider(
        Settings(
            generator=ModelSelection(provider="openai", model_id="gpt-x"),
            openai_api_key=SecretStr("k"),
        )
    )
    anthropic = build_provider(
        Settings(
            generator=ModelSelection(provider="anthropic", model_id="claude-x"),
            anthropic_api_key=SecretStr("k"),
        )
    )
    assert isinstance(openai, OpenAIProvider) and isinstance(anthropic, AnthropicProvider)
    assert openai.specification.provider == "openai"
    assert anthropic.specification.model_id == "claude-x"


def test_no_provider_falls_back_to_another_provider_or_model():
    config = ProviderConfig()
    assert config.fallback_policy == "NONE" and config.max_attempts == 1
    source = Path(ROOT, "backend/app/services/generation.py").read_text(encoding="utf-8")
    assert "except GenerationError" not in source


def _transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


def _openai(handler):
    return OpenAIProvider(
        ModelSelection(provider="openai", model_id="gpt-x"),
        ProviderConfig(),
        GroundingConfig(),
        SecretStr("secret-key"),
        _transport(handler),
    )


def _anthropic(handler):
    return AnthropicProvider(
        ModelSelection(provider="anthropic", model_id="claude-x"),
        ProviderConfig(),
        GroundingConfig(),
        SecretStr("secret-key"),
        _transport(handler),
    )


def _draft_payload(evidence_id):
    return {
        "answer": "Warfarin uses CYP2C9.",
        "claims": [{"text": "Warfarin uses CYP2C9.", "evidence_ids": [str(evidence_id)]}],
    }


def test_openai_adapter_returns_the_structured_draft():
    identifier = uuid4()
    import json as _json

    def handler(request):
        assert request.headers["Authorization"] == "Bearer secret-key"
        body = _json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["temperature"] == 0.0
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": _json.dumps(_draft_payload(identifier))}}]},
        )

    draft = asyncio.run(
        _openai(handler).generate_structured(
            system_policy="p", question="q", evidence="e", schema=ProviderDraft
        )
    )
    assert draft.claims[0].evidence_ids == [identifier]


def test_anthropic_adapter_forces_one_tool_for_structured_output():
    identifier = uuid4()
    import json as _json

    def handler(request):
        assert request.headers["x-api-key"] == "secret-key"
        body = _json.loads(request.content)
        assert body["tool_choice"] == {"type": "tool", "name": "record_grounded_draft"}
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "record_grounded_draft",
                        "input": _draft_payload(identifier),
                    }
                ]
            },
        )

    draft = asyncio.run(
        _anthropic(handler).generate_structured(
            system_policy="p", question="q", evidence="e", schema=ProviderDraft
        )
    )
    assert draft.answer == "Warfarin uses CYP2C9."


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "GENERATION_PROVIDER_AUTH_FAILED"),
        (403, "GENERATION_PROVIDER_AUTH_FAILED"),
        (429, "GENERATION_RATE_LIMITED"),
        (500, "GENERATION_PROVIDER_UNAVAILABLE"),
    ],
)
@pytest.mark.parametrize("build", [_openai, _anthropic])
def test_provider_http_failures_are_typed_and_fail_closed(status, code, build):
    provider = build(lambda request: httpx.Response(status, json={"error": "secret-key leaked?"}))
    with pytest.raises(GenerationError) as raised:
        asyncio.run(
            provider.generate_structured(
                system_policy="p", question="q", evidence="e", schema=ProviderDraft
            )
        )
    assert raised.value.code == code
    # The provider's own message can quote the prompt, which carries evidence text.
    assert "secret-key" not in str(raised.value.message)


@pytest.mark.parametrize("build", [_openai, _anthropic])
def test_provider_timeout_is_typed_and_retryable(build):
    def handler(request):
        raise httpx.TimeoutException("slow")

    with pytest.raises(GenerationError) as raised:
        asyncio.run(
            build(handler).generate_structured(
                system_policy="p", question="q", evidence="e", schema=ProviderDraft
            )
        )
    assert raised.value.code == "GENERATION_PROVIDER_TIMEOUT"
    assert raised.value.code in RETRYABLE


def test_malformed_provider_output_is_a_schema_violation_not_an_answer():
    import json as _json

    handler = lambda request: httpx.Response(  # noqa: E731
        200, json={"choices": [{"message": {"content": _json.dumps({"answer": "x"})}}]}
    )
    with pytest.raises(GenerationError, match="SCHEMA_VIOLATION"):
        asyncio.run(
            _openai(handler).generate_structured(
                system_policy="p", question="q", evidence="e", schema=ProviderDraft
            )
        )


def test_empty_provider_output_is_rejected():
    handler = lambda request: httpx.Response(  # noqa: E731
        200, json={"choices": [{"message": {"content": "   "}}]}
    )
    with pytest.raises(GenerationError, match="EMPTY"):
        asyncio.run(
            _openai(handler).generate_structured(
                system_policy="p", question="q", evidence="e", schema=ProviderDraft
            )
        )


def test_vision_analysis_is_refused_by_both_adapters():
    """M7 approves no vision path, so the capability cannot be reached by accident."""
    for build in (_openai, _anthropic):
        provider = build(lambda request: httpx.Response(200, json={}))
        with pytest.raises(GenerationError, match="NOT_PERMITTED"):
            asyncio.run(
                provider.analyze_image(
                    system_policy="p", question="q", image=b"x", media_type="image/png"
                )
            )


def test_both_adapters_satisfy_the_provider_protocol():
    for provider in (
        _openai(lambda r: httpx.Response(200, json={})),
        _anthropic(lambda r: httpx.Response(200, json={})),
        FakeProvider(lambda q, e: None),
    ):
        for name in ("generate", "generate_structured", "analyze_image"):
            assert inspect.iscoroutinefunction(getattr(provider, name))
        assert provider.specification.prompt_version == "grounded-draft-v1"
