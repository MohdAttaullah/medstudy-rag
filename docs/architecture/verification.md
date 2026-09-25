# M8 claim verification

`POST /api/v1/retrieval/answer` requires `retrieval:search`, `generation:draft` and
`generation:verify`. It runs the whole M5–M7 pipeline, then verifies every material claim in the
resulting draft, then releases an answer or abstains.

```
question → M5 retrieval → M6 rerank + evidence → M7 gate → M7 draft
        → M8 claim extraction → deterministic checks → semantic verification → contradiction
        → PASS (verified answer) / REGENERATE_ONCE (one repair, re-verified) / ABSTAIN
```

An M7 draft that never reaches verification is never released. A gate that returned `INSUFFICIENT`
or `CONFLICTING` still abstains, and M8 does not re-open that decision or generate in the hope of
overcoming missing evidence.

## Claim extraction

Deterministic (`claim-extraction-m8-v1`), over `draft.answer` — the text a reader sees — not over
the generator's own claim list. Declared claims supply citations to the propositions they cover;
a material proposition matching none keeps an empty citation list and fails `CLAIM_NOT_CITED`.
This is what stops a generator shipping a sentence it never bound to evidence.

Sentences split on terminators, with decimals and common abbreviations protected, then on
coordinators when both halves independently carry content. Splitting is biased toward
over-splitting: an extra fragment costs one verification, while a proposition welded to a supported
one is how a half-supported statement passes whole. Connective fragments are non-material and never
block an answer.

A coordination is split **only when each half is a proposition the generator itself declared and
bound to evidence**. A half that matches no declaration is not a clause the sentence contains; it
is a fragment the splitter invented, and failing it for carrying no citation would blame the
generator for the cut. Undeclared text is not thereby excused: it stays inside the sentence it was
written in and is verified there, where the whole-statement rule refuses it unless the evidence
establishes all of it. Three further constructions are never split — a coordination inside an
unclosed relative clause ("the surface **that** faces and conforms to…"), one bound by
"respectively" (which pairs two lists, so neither list stands alone), and one whose halves do not
each carry enough distinct words to be a clause. Counting used the *index* term stream before,
which emits a capitalised word twice for IDF, so "Midline anterior" counted three terms and passed
for a clause.

A citation marker written into the prose — a bracketed evidence_id, or "[2]" — carries no
proposition and is never material. The generator is separately instructed not to write one.

Each claim also records the sentence it came from, and the sentence before it. That context is
passed to the semantic verifier to resolve a pronoun or an elided subject, is labelled as not
evidence, and is never itself verified.

Claim types — `NUMERIC`, `NEGATED`, `QUALIFIED`, `TABLE_DERIVED`, `FORMULA_DERIVED`,
`VISUAL_DEPENDENT`, `ASSESSMENT_DERIVED`, `FACTUAL` — decide which extra checks a claim must
survive.

## Deterministic checks, which bind

Run first; a claim they fail never reaches a model, and no model verdict can overturn one.

- **Citations**: every id must be in *this* request's EvidenceSet. Existing elsewhere is not the
  test. Provenance must resolve — document, version, parse run, pages, elements and internally
  valid spans.
- **Numeric**: `(value, normalized unit)` pairs asserted by the claim must appear in the cited
  evidence. Unit aliases are normalized (`milligrams` → `mg`); a differing value is
  `NUMERIC_MISMATCH`, the same value under a different unit is `UNIT_MISMATCH`.
- **Negation**: compared against the passages actually about the claim's subject, falling back to
  the whole block for structured evidence. Reported only on a genuine polarity disagreement.
- **Certainty**: a strong assertion (`causes`, `always`, `contraindicated`) fires
  `OVERSTATED_CERTAINTY` only when the cited evidence hedges and never states the strong form.
- **Structured evidence**: keyed off what the claim *cites*, not its type label. A cited
  `TABLE_PART` must carry header rows; a formula claim needs a FORMULA artifact; a claim resting on
  a figure returns `VISUAL_INTERPRETATION_REQUIRED`, because no approved path reads an original
  image and a caption is not the figure.

## Semantic verification

`ClaimVerifier` receives one claim and the evidence it cites — no corpus, no retrieval, no web
search, no rank, no score. Its prompt states pretrained knowledge is not evidence, requires the
whole statement to be established, and forbids inventing an id. Output is a strict schema; prose is
recorded for a reviewer and never used as the decision. Verdicts: `SUPPORTED`, `UNSUPPORTED`,
`CONTRADICTED`, `INSUFFICIENT_EVIDENCE`, `UNVERIFIABLE`.

`ModelClaimVerifier` reaches a provider through the same `LLMProvider` protocol as the generator,
so adapters remain the only place vendor detail lives. `FakeClaimVerifier` makes the whole flow
testable without a key or a network, and mirrors the real adapter's failure behaviour exactly.

**Independence.** `MEDRAG_VERIFIER__PROVIDER` / `MEDRAG_VERIFIER__MODEL_ID` select a verifier
distinct from the generator. Unset, the generator's model is reused —
`VerifierSpec.independent_of_generator` records which happened, and the inspector warns when it is
false, because a verifier that is the same model shares the generator's blind spots.

## Contradiction

Beyond M7's block-to-block comparison: a claim supported by its cited source and contradicted by a
source retrieval **retained but did not cite**; two reference-grade sources stating different values
in the same unit; and assessment-only support standing against a reference source. Competing
evidence is preserved and returned. Rank never breaks a tie, and authority orders which
disagreement is reportable rather than which source wins.

## Final policy and repair

`PASS` requires every material claim `SUPPORTED`, deterministic checks clean and no contradiction.
`REGENERATE_ONCE` is offered only when every failure is repairable — an unsupported, uncited,
overstated, mis-numbered or reversed claim a rewrite could remove. `ABSTAIN` covers everything
else, including any contradiction, any unresolvable citation and any verifier failure.

Repair is capped at one by type. It receives the same evidence, never more, plus the exact
statements that failed, and is told that removing an unsupported statement is success. The repaired
draft runs the **entire** flow again and is never released on the strength of having been repaired.
A second failure abstains.

## Contracts

`VerifiedAnswer` is built only behind a PASS and its `verified` field is `Literal[True]` — the one
place in the system that state exists. M7 drafts keep
`UNVERIFIED_AWAITING_CLAIM_VERIFICATION`. `answering_enabled` stays `Literal[False]`: releasing a
verified answer object is not the user-facing Ask experience, which is M9. Failed verdicts are
retained in the report, because an abstention that did not say what failed would be unauditable.

The client sends a question and optional filters; `extra="forbid"` rejects a submitted draft,
evidence ids, tenant id, verifier, or verification result with 422.

## Telemetry and persistence

`answer_verification_outcomes_total{outcome}` counts decisions; declared failures use
`retrieval_failures_total` with mode `VERIFIED_ANSWER`. Logs carry the correlation id, the outcome,
the bounded reason-code vocabulary and the repair count — never the question, claim text, evidence,
answer or any key. Nothing is persisted; the Alembic head stays `m5_hybrid_retrieval` and no empty
M8 migration exists. All five policies are frozen and fingerprinted and appear in every report.

See [ADR-013](../adr/013-m8-claim-verification.md), [sufficiency](sufficiency.md) and
[generation](generation.md).
