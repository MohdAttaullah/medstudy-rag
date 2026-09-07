"""M8 safety contracts: claim extraction, deterministic checks, verification and final policy.

The failure this milestone exists to prevent is a fluent, unsupported medical answer released as
verified. Almost every test below is a way of asking whether some plausible-looking draft can get
past the gate, and asserting that it cannot.
"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import ModelSelection, Settings
from app.core.verification_config import (
    ClaimExtractionConfig,
    ClaimVerificationConfig,
    ContradictionConfig,
    FinalVerificationConfig,
    RepairConfig,
)
from app.evidence.model import ArtifactRef, EvidenceBlock, SourceSpan
from app.generation.errors import GenerationError
from app.generation.grounding.model import DraftClaim, GroundedDraft, ProviderSpec
from app.verification import contradiction as contradiction_checks
from app.verification.claims import classify, extract
from app.verification.engine import decide, find_contradictions, verify_claims
from app.verification.model import (
    REPAIRABLE,
    ClaimVerification,
    VerificationError,
    VerifiedAnswer,
)
from app.verification.verifier import (
    SYSTEM_POLICY,
    FakeClaimVerifier,
    ModelClaimVerifier,
    VerifiableClaim,
    VerifierVerdict,
    build_verifier,
    render,
)
from pydantic import SecretStr, ValidationError

ROOT = Path(__file__).resolve().parents[2]


def block(
    text="Warfarin is metabolised by CYP2C9.",
    *,
    source_type="TEXTBOOK",
    authority="REFERENCE",
    chunk_type="TEXT_CHILD",
    version=None,
    artifacts=(),
    visual=False,
    evidence_id=None,
):
    element = uuid4()
    return EvidenceBlock(
        evidence_id=evidence_id or uuid4(),
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
        representation="m3-source-with-structural-labels-v1",
        artifacts=list(artifacts),
        question=None,
        expansion_reason="RERANKED_ANCHOR",
        token_count=max(1, len(text) // 4),
        requires_visual_evidence=visual,
    )


def draft(answer, claims):
    return GroundedDraft(
        draft_id=uuid4(),
        answer=answer,
        claims=[DraftClaim(text=t, evidence_ids=list(ids)) for t, ids in claims],
        cited_evidence_ids=[],
        uncited_evidence_ids=[],
        evidence_gap=None,
        query_hash="0" * 64,
        provider=ProviderSpec(
            provider="fake",
            model_id="generator-1",
            endpoint="memory://fake",
            temperature=None,
            max_output_tokens=1024,
            schema_version="grounded-draft-schema-v1",
            prompt_version="grounded-draft-v1",
        ),
        grounding_policy_version="grounding-m7-v1",
        grounding_policy_fingerprint="g",
        sufficiency_policy_fingerprint="s",
        durations_ms={},
    )


def claims_of(answer, claims, blocks):
    by_id = {str(b.evidence_id): b for b in blocks}
    return extract(draft(answer, claims), by_id, ClaimExtractionConfig())


def verify(claims, blocks, verifier=None, **overrides):
    return asyncio.run(
        verify_claims(
            claims,
            blocks,
            uuid4(),
            verifier if verifier is not None else FakeClaimVerifier(),
            ClaimVerificationConfig(**overrides),
        )
    )


# --- Claim extraction ---------------------------------------------------------------------------


def test_conjoined_propositions_are_split_so_one_cannot_carry_the_other():
    """ "A treats B and is safe in pregnancy" must not pass on the strength of its first half."""
    source = block()
    extracted = claims_of(
        "Drug A treats condition B and Drug A is safe in pregnancy.",
        [("Drug A treats condition B and Drug A is safe in pregnancy.", [source.evidence_id])],
        [source],
    )
    material = [c for c in extracted if c.material]
    assert len(material) == 2
    assert "safe in pregnancy" in material[1].text


def test_a_decimal_or_abbreviation_does_not_end_a_sentence():
    source = block(text="The dose is 7.5 mg daily, e.g. in adults.")
    extracted = claims_of(
        "The dose is 7.5 mg daily, e.g. in adults.",
        [("The dose is 7.5 mg daily, e.g. in adults.", [source.evidence_id])],
        [source],
    )
    assert len([c for c in extracted if c.material]) == 1


def test_connective_fragments_are_not_material():
    source = block()
    extracted = claims_of(
        "Therefore, warfarin is metabolised by CYP2C9.",
        [("Warfarin is metabolised by CYP2C9.", [source.evidence_id])],
        [source],
    )
    assert any(c.material for c in extracted)
    assert all(c.claim_type != "NON_MATERIAL" or not c.material for c in extracted)


def test_answer_prose_the_generator_never_declared_is_extracted_and_uncited():
    """The safety property: extraction reads the answer, not the generator's own claim list.

    A generator that writes a sentence and simply omits it from `claims` would otherwise ship an
    unverified medical statement inside a verified answer.
    """
    source = block()
    extracted = claims_of(
        "Warfarin is metabolised by CYP2C9. Warfarin also cures hypertension.",
        [("Warfarin is metabolised by CYP2C9.", [source.evidence_id])],
        [source],
    )
    smuggled = [c for c in extracted if "hypertension" in c.text]
    assert smuggled and smuggled[0].material
    assert smuggled[0].cited_evidence_ids == []
    results = verify(extracted, [source])
    failed = [v for v in results if v.failed]
    assert [v.reason_codes for v in failed] == [["CLAIM_NOT_CITED"]]


def test_claim_types_drive_the_extra_checks_each_claim_must_survive():
    assert classify("The dose is 5 mg daily.", [block()], True) == "NUMERIC"
    assert classify("It is not recommended.", [block()], True) == "NEGATED"
    assert classify("It may be associated with rash.", [block()], True) == "QUALIFIED"
    assert classify("Row two lists it.", [block(chunk_type="TABLE_PART")], True) == "TABLE_DERIVED"
    assert classify("The figure shows it.", [block(visual=True)], True) == "VISUAL_DEPENDENT"
    assert classify("Anything.", [block()], False) == "NON_MATERIAL"


# --- Deterministic checks come first and bind ----------------------------------------------------


def test_a_wrong_dose_is_caught_without_asking_a_model():
    source = block(text="The maintenance dose is 5 mg daily.")
    extracted = claims_of(
        "The maintenance dose is 50 mg daily.",
        [("The maintenance dose is 50 mg daily.", [source.evidence_id])],
        [source],
    )
    verifier = FakeClaimVerifier(lambda c: VerifierVerdict(verdict="SUPPORTED"))
    results = verify(extracted, [source], verifier)
    failed = [v for v in results if v.failed]
    assert failed and "NUMERIC_MISMATCH" in failed[0].reason_codes
    assert failed[0].verdict == "CONTRADICTED"
    # The model said SUPPORTED and was never asked: a fact you can look up is not an opinion.
    assert verifier.seen == []


def test_a_wrong_unit_is_distinguished_from_a_wrong_value():
    source = block(text="The dose is 5 mg daily.")
    extracted = claims_of(
        "The dose is 5 mL daily.", [("The dose is 5 mL daily.", [source.evidence_id])], [source]
    )
    failed = [v for v in verify(extracted, [source]) if v.failed]
    assert failed and "UNIT_MISMATCH" in failed[0].reason_codes


def test_equivalent_unit_spellings_are_accepted():
    source = block(text="The dose is 5 milligrams daily.")
    extracted = claims_of(
        "The dose is 5 mg daily.", [("The dose is 5 mg daily.", [source.evidence_id])], [source]
    )
    assert not [v for v in verify(extracted, [source]) if v.failed]


def test_a_reversed_negation_fails():
    source = block(text="Amoxicillin is not recommended in this setting.")
    extracted = claims_of(
        "Amoxicillin is recommended in this setting.",
        [("Amoxicillin is recommended in this setting.", [source.evidence_id])],
        [source],
    )
    failed = [v for v in verify(extracted, [source]) if v.failed]
    assert failed and "NEGATION_REVERSED" in failed[0].reason_codes


def test_a_preserved_negation_passes():
    source = block(text="Amoxicillin is not recommended in this setting.")
    extracted = claims_of(
        "Amoxicillin is not recommended in this setting.",
        [("Amoxicillin is not recommended in this setting.", [source.evidence_id])],
        [source],
    )
    assert not [v for v in verify(extracted, [source]) if v.failed]


def test_a_hedged_source_does_not_support_an_unhedged_claim():
    """ "may be associated with" is not "causes", and the difference is the whole claim."""
    source = block(text="Ibuprofen may be associated with gastric irritation in some patients.")
    extracted = claims_of(
        "Ibuprofen causes gastric irritation in patients.",
        [("Ibuprofen causes gastric irritation in patients.", [source.evidence_id])],
        [source],
    )
    failed = [v for v in verify(extracted, [source]) if v.failed]
    assert failed and "OVERSTATED_CERTAINTY" in failed[0].reason_codes


def test_a_source_that_already_states_the_strong_form_supports_it():
    source = block(text="Ibuprofen causes gastric irritation in susceptible patients.")
    extracted = claims_of(
        "Ibuprofen causes gastric irritation in patients.",
        [("Ibuprofen causes gastric irritation in patients.", [source.evidence_id])],
        [source],
    )
    assert not [v for v in verify(extracted, [source]) if v.failed]


def test_an_invented_evidence_id_fails_closed():
    source = block()
    extracted = claims_of(
        "Warfarin is metabolised by CYP2C9.",
        [("Warfarin is metabolised by CYP2C9.", [uuid4()])],
        [source],
    )
    failed = [v for v in verify(extracted, [source]) if v.failed]
    assert failed and "UNKNOWN_CITATION" in failed[0].reason_codes
    assert failed[0].verdict == "UNVERIFIABLE"


def test_a_block_with_unresolvable_provenance_fails():
    source = block()
    broken = source.model_copy(update={"pages": []})
    extracted = claims_of(
        "Warfarin is metabolised by CYP2C9.",
        [("Warfarin is metabolised by CYP2C9.", [broken.evidence_id])],
        [broken],
    )
    failed = [v for v in verify(extracted, [broken]) if v.failed]
    assert failed and "PROVENANCE_UNRESOLVED" in failed[0].reason_codes


def test_a_table_claim_without_header_rows_fails():
    """M6 measures a real table gap; the verifier must not reconstruct the missing structure."""
    headerless = ArtifactRef(
        artifact_id=uuid4(), kind="TABLE", source_element_id=uuid4(), href="/a", row_indexes=[2]
    )
    source = block(chunk_type="TABLE_PART", artifacts=[headerless], text="Row two lists 5 mg.")
    extracted = claims_of(
        "Row two lists the interval.",
        [("Row two lists the interval.", [source.evidence_id])],
        [source],
    )
    failed = [v for v in verify(extracted, [source]) if v.failed]
    assert failed and "PROVENANCE_UNRESOLVED" in failed[0].reason_codes


def test_a_visual_claim_abstains_because_no_approved_vision_verifier_exists():
    source = block(chunk_type="FIGURE_CONTEXT", visual=True, text="Figure 9.1 Cardiac cycle.")
    extracted = claims_of(
        "The figure shows the cardiac cycle pressures.",
        [("The figure shows the cardiac cycle pressures.", [source.evidence_id])],
        [source],
    )
    failed = [v for v in verify(extracted, [source]) if v.failed]
    assert failed and "VISUAL_INTERPRETATION_REQUIRED" in failed[0].reason_codes


# --- The semantic verifier can only ever fail a claim -------------------------------------------


def test_the_verifier_receives_only_the_claim_and_its_cited_evidence():
    source, other = block(), block(text="An unrelated passage about something else.")
    extracted = claims_of(
        "Warfarin is metabolised by CYP2C9.",
        [("Warfarin is metabolised by CYP2C9.", [source.evidence_id])],
        [source, other],
    )
    verifier = FakeClaimVerifier()
    verify(extracted, [source, other], verifier)
    assert verifier.seen and [b.evidence_id for b in verifier.seen[0].evidence] == [
        source.evidence_id
    ]


def test_an_unsupported_semantic_verdict_fails_the_claim():
    source = block()
    extracted = claims_of(
        "Warfarin is metabolised by CYP2C9.",
        [("Warfarin is metabolised by CYP2C9.", [source.evidence_id])],
        [source],
    )
    verifier = FakeClaimVerifier(lambda c: VerifierVerdict(verdict="UNSUPPORTED"))
    failed = [v for v in verify(extracted, [source], verifier) if v.failed]
    assert failed and failed[0].reason_codes == ["SEMANTICALLY_UNSUPPORTED"]


def test_a_verifier_naming_evidence_it_was_not_given_is_rejected():
    verifier = ModelClaimVerifier(
        _provider(VerifierVerdict(verdict="SUPPORTED", supporting_evidence_ids=[uuid4()])),
        independent=True,
    )
    with pytest.raises(VerificationError, match="VERIFIER_UNKNOWN_EVIDENCE"):
        asyncio.run(
            verifier.verify(VerifiableClaim(claim_id=uuid4(), text="A claim.", evidence=[block()]))
        )


def test_verifier_provider_failure_fails_closed():
    verifier = ModelClaimVerifier(
        _provider(GenerationError("GENERATION_PROVIDER_TIMEOUT")), independent=True
    )
    with pytest.raises(VerificationError, match="VERIFIER_FAILED"):
        asyncio.run(
            verifier.verify(VerifiableClaim(claim_id=uuid4(), text="A claim.", evidence=[block()]))
        )


def test_a_missing_verifier_fails_closed_rather_than_passing():
    source = block()
    extracted = claims_of(
        "Warfarin is metabolised by CYP2C9.",
        [("Warfarin is metabolised by CYP2C9.", [source.evidence_id])],
        [source],
    )
    with pytest.raises(VerificationError, match="VERIFIER_FAILED"):
        asyncio.run(verify_claims(extracted, [source], uuid4(), None, ClaimVerificationConfig()))


class _Provider:
    def __init__(self, result):
        self.result = result

    @property
    def specification(self):
        return ProviderSpec(
            provider="openai",
            model_id="verifier-model",
            endpoint="https://example.invalid",
            temperature=None,
            max_output_tokens=1024,
            schema_version="grounded-draft-schema-v1",
            prompt_version="grounded-draft-v1",
        )

    async def generate(self, **kwargs):
        raise NotImplementedError

    async def generate_structured(self, **kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def analyze_image(self, **kwargs):
        raise NotImplementedError


def _provider(result):
    return _Provider(result)


def test_the_verifier_policy_forbids_pretrained_knowledge_and_demands_whole_claims():
    assert "Pretrained model knowledge is not evidence." in SYSTEM_POLICY
    assert "Judge the whole statement" in SYSTEM_POLICY
    assert "5 mg and 50 mg" in SYSTEM_POLICY
    assert "Never invent one." in SYSTEM_POLICY


def test_rendered_verifier_input_carries_no_rank_or_score():
    rendered = render(VerifiableClaim(claim_id=uuid4(), text="A claim.", evidence=[block()]))
    assert "evidence_id=" in rendered and "authority REFERENCE" in rendered
    for forbidden in ("rank", "score", "logit"):
        assert forbidden not in rendered.lower()


# --- Contradiction ------------------------------------------------------------------------------


def test_a_claim_contradicted_by_uncited_retained_evidence_is_found():
    """The shape a generator creates by citing only the source that agrees with it."""
    cited = block(text="The loading dose is 5 mg.")
    retained = block(text="The loading dose is 10 mg.")
    extracted = claims_of(
        "The loading dose is 5 mg.",
        [("The loading dose is 5 mg.", [cited.evidence_id])],
        [cited, retained],
    )
    findings = find_contradictions(extracted, [cited, retained], ContradictionConfig())
    assert any(f.kind == "CONTRADICTED_BY_RETAINED_EVIDENCE" for f in findings)


def test_two_reference_sources_disagreeing_is_reported_without_choosing_one():
    a = block(text="The target is 130 mmHg.")
    b = block(text="The target is 140 mmHg.")
    findings = contradiction_checks.authoritative_disagreement([a, b])
    assert findings and findings[0].kind == "AUTHORITATIVE_SOURCES_DISAGREE"
    assert set(findings[0].evidence_ids) == {a.evidence_id, b.evidence_id}


def test_one_document_stating_several_values_is_a_range_not_a_disagreement():
    version = uuid4()
    a = block(text="The target is 130 mmHg.", version=version)
    b = block(text="The target is 140 mmHg.", version=version)
    assert contradiction_checks.authoritative_disagreement([a, b]) == []


def test_assessment_only_support_against_a_reference_source_is_reported():
    key = block(
        text="The recorded answer is 300 mg.",
        source_type="ANSWER_KEY",
        authority="ASSESSMENT",
        chunk_type="QUESTION",
    )
    reference = block(text="The recorded answer is 150 mg.")
    extracted = claims_of(
        "The recorded answer is 300 mg.",
        [("The recorded answer is 300 mg.", [key.evidence_id])],
        [key, reference],
    )
    findings = find_contradictions(extracted, [key, reference], ContradictionConfig())
    assert any(
        f.kind in {"ASSESSMENT_CONTRADICTS_REFERENCE", "CONTRADICTED_BY_RETAINED_EVIDENCE"}
        for f in findings
    )


def test_rank_never_breaks_a_tie():
    config = ContradictionConfig()
    assert config.rank_may_break_ties is False
    assert config.assessment_cannot_override_reference is True
    source = Path(ROOT, "backend/app/verification/contradiction.py").read_text(encoding="utf-8")
    for forbidden in ("reranker_score", "fused_score", "reranked_rank", "fused_rank"):
        assert forbidden not in source


# --- Final policy -------------------------------------------------------------------------------


def supported(**overrides):
    base = dict(
        claim_id=uuid4(),
        claim_text="A claim.",
        claim_type="FACTUAL",
        material=True,
        verdict="SUPPORTED",
        reason_codes=["SUPPORTED_BY_CITED_EVIDENCE"],
    )
    return ClaimVerification(**{**base, **overrides})


def test_all_supported_claims_pass():
    outcome, codes = decide([supported()], [], 0, True, FinalVerificationConfig())
    assert outcome == "PASS" and codes == []


def test_a_repairable_failure_offers_exactly_one_repair():
    failure = supported(verdict="UNSUPPORTED", reason_codes=["SEMANTICALLY_UNSUPPORTED"])
    first, _ = decide([failure], [], 0, True, FinalVerificationConfig())
    assert first == "REGENERATE_ONCE"
    # After one repair the same failure abstains; there is no second attempt.
    second, _ = decide([failure], [], 1, True, FinalVerificationConfig())
    assert second == "ABSTAIN"


def test_an_unrepairable_failure_abstains_immediately():
    for code in ("UNKNOWN_CITATION", "PROVENANCE_UNRESOLVED", "VISUAL_INTERPRETATION_REQUIRED"):
        failure = supported(verdict="UNVERIFIABLE", reason_codes=[code])
        outcome, _ = decide([failure], [], 0, True, FinalVerificationConfig())
        assert outcome == "ABSTAIN", code
        assert code not in REPAIRABLE


def test_a_contradiction_abstains_and_is_never_repaired():
    """Rewriting cannot settle a corpus that disagrees with itself, and choosing is banned."""
    finding = contradiction_checks.authoritative_disagreement(
        [block(text="The target is 130 mmHg."), block(text="The target is 140 mmHg.")]
    )
    outcome, codes = decide([supported()], finding, 0, True, FinalVerificationConfig())
    assert outcome == "ABSTAIN" and "AUTHORITATIVE_SOURCES_DISAGREE" in codes


def test_a_non_material_claim_never_blocks_an_answer():
    outcome, _ = decide(
        [
            supported(),
            supported(material=False, verdict="UNSUPPORTED", reason_codes=["NON_MATERIAL_CLAIM"]),
        ],
        [],
        0,
        True,
        FinalVerificationConfig(),
    )
    assert outcome == "PASS"


def test_repair_is_capped_by_type():
    assert RepairConfig().max_attempts == 1
    assert RepairConfig().may_widen_evidence is False


# --- verified=true exists in exactly one place ---------------------------------------------------


def test_only_a_verified_answer_can_carry_verified_true():
    assert VerifiedAnswer.model_fields["verified"].annotation.__args__ == (True,)
    with pytest.raises(ValidationError):
        VerifiedAnswer.model_validate(
            {
                "answer_id": uuid4(),
                "verified": False,
                "answer": "x",
                "claims": [],
                "cited_evidence_ids": [],
                "query_hash": "0" * 64,
                "draft": draft("x", []).model_dump(mode="json"),
                "generator": draft("x", []).provider.model_dump(mode="json"),
                "verifier": FakeClaimVerifier().specification.model_dump(mode="json"),
                "repair_count": 0,
            }
        )


def test_an_m7_draft_can_never_be_marked_verified():
    document = draft("x", [])
    assert document.verification_status == "UNVERIFIED_AWAITING_CLAIM_VERIFICATION"
    with pytest.raises(ValidationError):
        GroundedDraft.model_validate(
            {**document.model_dump(mode="json"), "verification_status": "VERIFIED"}
        )


def test_the_report_exposes_no_confidence_number():
    outcome, _ = decide([supported()], [], 0, True, FinalVerificationConfig())
    assert outcome == "PASS"
    dumped = supported().model_dump(mode="json")
    for forbidden in ("confidence", "probability", "score"):
        assert forbidden not in dumped


# --- Verifier independence ------------------------------------------------------------------------


def test_a_distinct_verifier_model_is_reported_as_independent():
    settings = Settings(
        generator=ModelSelection(provider="openai", model_id="generator-model"),
        verifier=ModelSelection(provider="openai", model_id="verifier-model"),
        openai_api_key=SecretStr("k"),
    )
    assert build_verifier(settings).specification.independent_of_generator is True


def test_falling_back_to_the_generator_is_reported_as_not_independent():
    """A verifier that is the generator shares its blind spots; the report must say so."""
    settings = Settings(
        generator=ModelSelection(provider="openai", model_id="generator-model"),
        openai_api_key=SecretStr("k"),
    )
    spec = build_verifier(settings).specification
    assert spec.model_id == "generator-model"
    assert spec.independent_of_generator is False


def test_no_configured_model_means_verification_is_unavailable():
    with pytest.raises(VerificationError, match="VERIFIER_FAILED"):
        build_verifier(Settings(generator=None, verifier=None))


def test_no_vendor_sdk_leaks_into_the_verification_layer():
    for path in Path(ROOT, "backend/app/verification").rglob("*.py"):
        body = path.read_text(encoding="utf-8").lower()
        assert "api.openai.com" not in body and "api.anthropic.com" not in body
        assert "import openai" not in body and "import anthropic" not in body


def test_policies_are_fingerprinted_so_a_decision_is_reconstructable():
    for config, other in (
        (ClaimExtractionConfig(), ClaimExtractionConfig(min_material_terms=5)),
        (ClaimVerificationConfig(), ClaimVerificationConfig(check_numeric_agreement=False)),
        (ContradictionConfig(), ContradictionConfig(check_uncited_retained_evidence=False)),
        (RepairConfig(), RepairConfig(enabled=False)),
    ):
        assert config.fingerprint != other.fingerprint
