# ADR-013: A draft becomes an answer only when every material claim survives verification

Status: Accepted for M8. Implements the ADR-004 verification contract on top of ADR-012's gate and ADR-005's provider boundary. Changes no stored schema, no ingestion state and no M5–M7 semantics.

## The claim set is taken from the answer, not from the generator's declarations

M7 already required a draft to bind statements to evidence. That is not enough on its own: a generator can write a sentence and simply leave it out of its own claim list, and everything downstream would then verify a subset of what a reader actually sees. M8 therefore extracts claims deterministically from `draft.answer`, and uses the declared claims only to supply citations to the propositions they cover. A material proposition matching no declared claim keeps an empty citation list, which fails as `CLAIM_NOT_CITED`. This is the single most important structural decision in the milestone.

Splitting is biased toward over-splitting, because the asymmetry runs one way: an extra fragment costs one more verification, while a proposition left welded to a supported one is exactly how "Drug A treats B **and is safe in pregnancy**" passes on the strength of its first half. Sentences split on terminators with decimals and common abbreviations protected, then on coordinators when both halves independently carry content. Connective fragments are non-material and never block an answer.

## Deterministic checks run first and bind

Whether a citation exists, whether its provenance resolves, whether a dose matches and whether a negation survived are decidable by looking. Asking a language model to adjudicate any of them would trade a certain answer for an uncertain one, and would let a fluent verifier wave through the two failure modes that hurt most in a medical answer: a wrong number and a reversed negation. So the deterministic layer runs first, and a claim it fails is never sent to the model. The model can only ever *fail* a claim that survived; it can never rescue one.

Numeric agreement compares `(value, normalized unit)` pairs, because the unit is part of the identity — 5 mg and 5 mL are not the same measurement, and comparing bare numbers would call them equal. A differing value is `NUMERIC_MISMATCH`; the same value under a different unit is `UNIT_MISMATCH`, distinguished so a reader knows which half was wrong. Negation is compared against the passages actually about the claim's subject, and reported only on a genuine polarity disagreement. Certainty overstatement fires only when the evidence hedges and the claim does not, so a source already stating the strong form supports the strong claim.

Table, formula and figure checks key off what the claim **cites**, not off its single type label. An earlier version keyed them off the label and let a table without header rows through, because a claim quoting a number from a table is classified `NUMERIC` and the table branch never ran. That defect was found by the M8 evaluation and is why the check now inspects the cited blocks directly.

## Semantic verification is isolated and constrained

The verifier receives one claim and the evidence that claim cites. No corpus handle, no retrieval, no web search, no rank and no score. Its prompt states that pretrained knowledge is not evidence, requires the whole statement to be established rather than a fragment of it, and forbids inventing an evidence id. It returns a strict schema; free prose is recorded for a reviewer and never used as the machine decision. A verdict naming evidence nobody supplied is rejected outright, and any verifier failure — timeout, auth, rate limit, malformed output, unknown id — abstains. There is no path from a failed verification to a released answer.

The verifier is reached through the same `LLMProvider` protocol as the generator, so no new vendor surface exists and adapters remain the only place vendor detail lives.

## Generator and verifier independence is configurable, and honestly reported

`MEDRAG_VERIFIER__*` selects a verifier distinct from the generator. When it is unset the generator's model is used, which is a real weakness: a verifier that is the same model shares the generator's blind spots, so the claim it is least likely to fail is precisely the one the generator was most confident about. Requiring two paid accounts to make M8 testable would be worse, so the fallback is permitted and `VerifierSpec.independent_of_generator` records which situation produced a given answer. The inspector shows the warning in the non-independent case.

## Contradiction extends what M7 could see

M7 compared evidence blocks with each other and recorded that it missed prose-level contradictions. M8 adds the case a generator actually creates: a claim supported by the source it cited and contradicted by a source retrieval retained but the generator did not cite. It also reports two reference-grade sources stating different values in the same unit, and assessment-only support standing against a reference source. Rank never breaks these ties — the block a CrossEncoder put first is not thereby true — and source authority orders which disagreement is reportable, never which source wins.

## One repair, then stop

A failure a rewrite could remove offers exactly one repair. The repair receives the same evidence, never more, plus the specific statements that failed and why, and is instructed that removing an unsupported statement is success. It may only narrow the answer: widening retrieval would make the repaired draft answer a different question from the one the gate approved. The repaired draft then runs the entire flow again — extraction, deterministic checks, semantic verification, contradiction — and is never released on the strength of having been repaired. If it fails again, the request abstains. A loop would eventually produce a draft that survives by luck.

Contradictions and unresolvable citations are not repairable: the first is a property of the corpus and the second of the request, and neither changes when the answer is rewritten.

## `verified=true` exists in one place

`VerifiedAnswer` is constructed only behind a PASS, and its `verified` field is `Literal[True]`, so no other object in the system can claim the state. M7 drafts keep `UNVERIFIED_AWAITING_CLAIM_VERIFICATION` and are not retroactively mutated. `answering_enabled` stays `Literal[False]`: releasing a verified answer object is not the same as switching on the user-facing Ask experience, which is M9. Every failed verdict is retained in the report rather than dropped, because an abstention that did not say what failed would be unauditable.

## Persistence and consequences

Nothing is stored: no question, claim, verdict or answer. The Alembic head stays `m5_hybrid_retrieval` and no empty migration is created. Every policy is frozen and fingerprinted, and the report carries all five fingerprints, so why an answer passed or abstained can be reconstructed without persisting its content.

Coverage falls again, deliberately. On the M8 fixture 15 of 19 cases abstain. The deterministic checks are lexical and will miss a contradiction expressed in words that do not overlap, and will occasionally fire on text where a shared label and unit coincide. No threshold in any of this is calibrated, the fixture was written alongside the checks it measures, and the false-PASS count is reported on its own because it is not interchangeable with an unnecessary abstention: one emits a wrong medical answer and the other emits nothing.
