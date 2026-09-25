"""A claim must be a proposition the draft actually asserts, and one the generator actually cited.

The measured failure: "What is the tentorial surface of the cerebellum?" abstained as UNVERIFIED on
a corpus whose page says "The tentorial surface faces and conforms to the lower surface of the
tentorium" — retrieved at rank 1 by both lanes. Nothing was wrong with retrieval, the evidence gate
or the verifier. Claim extraction cut the draft into pieces the draft never asserted:

* "Midline anterior" — the sparse analyzer emits a case-exact duplicate of a capitalised word for
  IDF, so a two-word fragment counted three "content terms" and passed for an independent clause.
  The noun phrase "Midline anterior and posterior cerebellar incisurae" was split across its own
  subject, and the remainder matched no declared claim, so it carried no citation and failed
  CLAIM_NOT_CITED — a mutilation blamed on the generator.
* "[1e5fdd99-6e8b-5c24-93b6-5c6202ae8c66]" — a citation marker the generator wrote into the prose
  became its own sentence, its hex groups counted as content, and a pointer to a source was
  verified as a material medical claim.
* "The tentorial surface is the cerebellar surface that faces" — a coordination inside a relative
  clause, split into something that asserts nothing.

What must not change: text the generator never declared is still verified. It stays attached to the
sentence it was written in, so a draft that adds an unsupported half still has to survive the
verifier judging that sentence whole.
"""

from uuid import UUID, uuid4, uuid5

import pytest
from app.core.verification_config import ClaimExtractionConfig
from app.generation.grounding.model import DraftClaim, GroundedDraft
from app.verification.claims import extract

EVIDENCE = uuid5(UUID(int=3), "tentorial-evidence")
OTHER = uuid5(UUID(int=3), "second-evidence")


def draft(answer: str, claims: list[tuple[str, list[UUID]]]) -> GroundedDraft:
    return GroundedDraft(
        draft_id=uuid4(),
        answer=answer,
        claims=[DraftClaim(text=text, evidence_ids=ids) for text, ids in claims],
        cited_evidence_ids=sorted({i for _, ids in claims for i in ids}),
        uncited_evidence_ids=[],
        evidence_gap=None,
        query_hash="h",
        provider={
            "provider": "fake",
            "model_id": "test",
            "endpoint": "local",
            "temperature": None,
            "max_output_tokens": 1024,
            "prompt_version": "grounded-draft-v2",
            "schema_version": "grounded-draft-schema-v1",
        },
        grounding_policy_version="grounding-m7-v1",
        grounding_policy_fingerprint="f",
        sufficiency_policy_fingerprint="f",
        durations_ms={},
    )


@pytest.fixture
def config():
    return ClaimExtractionConfig()


def texts(result):
    return [claim.text for claim in result]


# ------------------------------------------------- the three cuts that abstained a good answer


def test_a_noun_phrase_coordination_is_not_split_across_its_subject(config):
    """The decisive defect. "Midline anterior" is two words, not an independent clause."""
    sentence = (
        "Midline anterior and posterior cerebellar incisurae accommodate the brainstem and falx "
        "cerebelli, respectively."
    )
    claims = extract(
        draft(
            sentence,
            [
                (
                    "The midline anterior cerebellar incisura accommodates the brainstem.",
                    [EVIDENCE],
                ),
                (
                    "The midline posterior cerebellar incisura accommodates the falx cerebelli.",
                    [EVIDENCE],
                ),
            ],
        ),
        {},
        config,
    )
    assert "Midline anterior" not in texts(claims)
    assert all(len(claim.text.split()) > 2 for claim in claims if claim.material)


def test_splitting_never_manufactures_an_uncited_claim(config):
    """CLAIM_NOT_CITED must accuse the generator, never the splitter.

    Every material claim here has to carry a citation, because every one of them is a piece of a
    sentence the generator bound to evidence.
    """
    sentence = (
        "Midline anterior and posterior cerebellar incisurae accommodate the brainstem and falx "
        "cerebelli, respectively."
    )
    claims = extract(
        draft(
            sentence,
            [
                (
                    "The midline anterior cerebellar incisura accommodates the brainstem.",
                    [EVIDENCE],
                ),
                (
                    "The midline posterior cerebellar incisura accommodates the falx cerebelli.",
                    [OTHER],
                ),
            ],
        ),
        {},
        config,
    )
    material = [claim for claim in claims if claim.material]
    assert material
    for claim in material:
        assert claim.cited_evidence_ids, f"{claim.text!r} was cut into an uncitable fragment"


def test_a_citation_marker_is_not_a_medical_claim(config):
    """A pointer to a source asserts nothing, so it can be neither supported nor uncited."""
    answer = "The tentorial surface faces the tentorium. [1e5fdd99-6e8b-5c24-93b6-5c6202ae8c66]"
    claims = extract(
        draft(answer, [("The tentorial surface faces the tentorium.", [EVIDENCE])]), {}, config
    )
    marker = [claim for claim in claims if "1e5fdd99" in claim.text]
    assert marker, "the marker is still extracted, so its handling stays visible"
    assert not marker[0].material, "a citation marker must never be a material claim"
    assert [claim.text for claim in claims if claim.material] == [
        "The tentorial surface faces the tentorium."
    ]


@pytest.mark.parametrize("marker", ["[2]", "[17]", "[1e5fdd99-6e8b-5c24-93b6-5c6202ae8c66]"])
def test_no_marker_shape_becomes_material(config, marker):
    claims = extract(
        draft(
            f"The surface slopes downward. {marker}", [("The surface slopes downward.", [EVIDENCE])]
        ),
        {},
        config,
    )
    assert not [c for c in claims if marker in c.text and c.material]


def test_a_coordination_inside_a_relative_clause_stays_whole(config):
    """ "...the surface that faces and conforms to..." is one relative clause, not two."""
    sentence = (
        "The tentorial surface is the cerebellar surface that faces and conforms to the lower "
        "surface of the tentorium."
    )
    claims = extract(draft(sentence, [(sentence, [EVIDENCE])]), {}, config)
    assert texts(claims) == [sentence]


def test_a_closed_relative_clause_does_not_block_a_real_split(config):
    """The guard is about an *unclosed* relative clause; a completed one splits as before."""
    sentence = (
        "Drug A, which is a beta blocker, treats resistant hypertension and reduces mortality "
        "after myocardial infarction."
    )
    claims = extract(
        draft(
            sentence,
            [
                ("Drug A treats resistant hypertension.", [EVIDENCE]),
                ("Drug A reduces mortality after myocardial infarction.", [OTHER]),
            ],
        ),
        {},
        config,
    )
    assert len(claims) > 1, "a coordination between two declared propositions still splits"
    assert all(claim.cited_evidence_ids for claim in claims if claim.material)


# --------------------------------------------------------- what must not be weakened


def test_an_undeclared_half_is_still_verified_and_cannot_ride_along(config):
    """The safety property the splitting exists for.

    The generator declares only the first half and writes a second the evidence never covered.
    That text may not vanish: it stays in the claim, so the verifier — whose first rule refuses a
    statement whose evidence establishes only part of it — has to judge it.
    """
    answer = "Drug A treats hypertension and is safe in pregnancy."
    claims = extract(draft(answer, [("Drug A treats hypertension.", [EVIDENCE])]), {}, config)
    material = [claim for claim in claims if claim.material]
    assert any("safe in pregnancy" in claim.text for claim in material), (
        "undeclared text must remain inside a verified claim"
    )
    assert "".join(claim.text for claim in claims).count("safe in pregnancy") == 1


def test_a_sentence_the_generator_never_declared_is_still_extracted(config):
    """Extraction runs over the answer, not over the declarations. That does not change."""
    answer = "The tentorial surface faces the tentorium. Warfarin is contraindicated in pregnancy."
    claims = extract(
        draft(answer, [("The tentorial surface faces the tentorium.", [EVIDENCE])]), {}, config
    )
    smuggled = [c for c in claims if "Warfarin" in c.text]
    assert smuggled and smuggled[0].material
    assert not smuggled[0].cited_evidence_ids, "an undeclared sentence carries no citation"


def test_every_claim_still_maps_to_a_span_of_the_answer(config):
    answer = (
        "The tentorial surface faces the tentorium. Its apex is the highest point of the "
        "cerebellum and the surface slopes downward from it."
    )
    result = extract(
        draft(
            answer,
            [
                ("The tentorial surface faces the tentorium.", [EVIDENCE]),
                ("Its apex is the highest point of the cerebellum.", [EVIDENCE]),
                ("The surface slopes downward from the apex.", [EVIDENCE]),
            ],
        ),
        {},
        config,
    )
    for claim in result:
        assert answer[claim.start : claim.end] == claim.text


def test_each_claim_carries_the_sentence_it_came_from(config):
    """The verifier resolves an elided subject from this, and from nothing else."""
    answer = "Its apex is formed by the anterior vermis and is the highest point on the cerebellum."
    claims = extract(
        draft(
            answer,
            [
                ("The apex is formed by the anterior vermis.", [EVIDENCE]),
                ("The apex is the highest point on the cerebellum.", [EVIDENCE]),
            ],
        ),
        {},
        config,
    )
    for claim in claims:
        assert claim.context == answer.strip()


# ------------------------------------------------------------- citation binding


def test_a_sentence_merging_two_declared_claims_is_attributed_to_both(config):
    """Attribution reads in both directions, so a merged sentence is not orphaned."""
    sentence = "The anterior incisura holds the brainstem and the posterior incisura the falx."
    claims = extract(
        draft(
            sentence,
            [
                ("The anterior incisura holds the brainstem.", [EVIDENCE]),
                ("The posterior incisura holds the falx.", [OTHER]),
            ],
        ),
        {},
        config,
    )
    cited = {evidence for claim in claims for evidence in claim.cited_evidence_ids}
    assert cited == {EVIDENCE, OTHER}


def test_attribution_does_not_excuse_extra_assertions(config):
    """A declared claim read into a longer sentence still leaves the whole sentence to be judged."""
    answer = "Drug A treats hypertension in every patient without exception."
    claims = extract(draft(answer, [("Drug A treats hypertension.", [EVIDENCE])]), {}, config)
    assert len(claims) == 1
    assert claims[0].material and claims[0].cited_evidence_ids == [EVIDENCE]
    # The extra assertion is inside the text the verifier receives, not silently dropped.
    assert "without exception" in claims[0].text


# ------------------------------------------------- a coordination bound by "respectively"


def test_a_respectively_sentence_is_never_split(config):
    """Splitting it produced a statement the draft did not make, and the verifier refused it.

    "...incisurae accommodate the brainstem and falx cerebelli, respectively" cut at the second
    coordinator leaves "...incisurae accommodate the brainstem", which says both incisurae hold the
    brainstem. The source says the falx holds the posterior one — a contradiction the split
    invented.
    """
    declared = [
        ("The anterior incisura accommodates the brainstem.", [EVIDENCE]),
        ("The posterior incisura accommodates the falx cerebelli.", [OTHER]),
    ]
    body = (
        "The anterior incisura accommodates the brainstem and the posterior incisura "
        "accommodates the falx cerebelli"
    )
    bound = f"{body}, respectively."
    claims = extract(draft(bound, declared), {}, config)
    assert texts(claims) == [bound]

    # The control: the same coordination, both halves declared and both well-formed clauses, is
    # split exactly as before. "respectively" is the whole difference.
    loose = extract(draft(f"{body}.", declared), {}, config)
    assert len(loose) == 2


def test_a_sentence_without_respectively_is_unaffected(config):
    sentence = "Drug A lowers blood pressure and Drug B lowers resting heart rate."
    claims = extract(
        draft(
            sentence,
            [
                ("Drug A lowers blood pressure.", [EVIDENCE]),
                ("Drug B lowers resting heart rate.", [OTHER]),
            ],
        ),
        {},
        config,
    )
    assert len(claims) == 2


def test_context_reaches_back_one_sentence_for_a_pronoun_subject(config):
    """A verifier refused "It contains the vermis..." for having no antecedent. Now it has one."""
    answer = "The tentorial surface faces the tentorium. It contains the vermis in the midline."
    claims = extract(
        draft(
            answer,
            [
                ("The tentorial surface faces the tentorium.", [EVIDENCE]),
                ("The tentorial surface contains the vermis in the midline.", [EVIDENCE]),
            ],
        ),
        {},
        config,
    )
    pronoun = next(claim for claim in claims if claim.text.startswith("It contains"))
    assert "The tentorial surface faces the tentorium." in pronoun.context
    assert pronoun.context.endswith("It contains the vermis in the midline.")
    # The first sentence has nothing before it, so its context is only itself.
    first = claims[0]
    assert first.context == "The tentorial surface faces the tentorium."
