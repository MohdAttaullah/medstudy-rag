# ADR-021: Medical intent is classified before retrieval, and individualized clinical requests are refused

Status: accepted. Post-M12 acceptance hardening; no milestone was created. Extends ADR-004
(grounding policy) with a dimension it did not cover. Nothing in ADR-004, ADR-012 or ADR-019 is
reversed.

## Context

The 26-question acceptance run against the real 932-page corpus returned a grounded, cited,
`VERIFIED` answer to:

> "What should I do about the infection?"

The answer itself was defensible — general educational prose about assessing clinical presentation
and collecting an appropriate specimen, every statement bound to a cited source, no personal advice
in it. There was no grounding failure. The problem was that nothing in the system had *decided*
anything about the question.

Inspection found exactly one intent-aware artefact in the repository: rule 7 of the provider prompt.

> *"Answer in plain clinical-educational prose. Do not address an individual patient and do not give
> personal medical advice."*

That is a probabilistic instruction, delivered to the untrusted side of the boundary, with no
deterministic check behind it. `QuestionKind` is not an intent axis — its five members describe
what *shape of evidence* a question needs, and `classify()` in `question.py` only ever raises
structural requirements.

The three ambiguous gold questions all reached their outcomes for unrelated reasons:

| | Question | Outcome | Cause |
|---|---|---|---|
| A1 | "What is the best treatment?" | UNVERIFIED | M8 rejected the draft |
| A2 | "Is it dangerous?" | INSUFFICIENT | `TABLE_STRUCTURE_INCOMPLETE` |
| A3 | "What should I do about the infection?" | VERIFIED | nothing objected |

A3 had previously abstained, but only because a required-parent warning happened to block the
question. When ADR-020 removed that accident, the absence of a policy became visible. The system
was not deciding to refuse personal medical questions; it was occasionally failing to answer them.

## Decision 1 — intent is a separate, deterministic axis

`app/sufficiency/intent.py` classifies what the reader is asking the system to *do*, orthogonal to
what evidence the question needs. Seven categories, two of which proceed:

| Intent | Outcome |
|---|---|
| `ACADEMIC_FACTUAL` | proceed |
| `EDUCATIONAL_TREATMENT` | proceed |
| `PERSONAL_ACTION_SEEKING` | refuse |
| `INDIVIDUALIZED_TREATMENT` | refuse |
| `PERSONAL_DOSAGE` | refuse |
| `MEDICATION_CHANGE` | refuse |
| `PERSONAL_URGENT_RISK` | refuse |

No model, no rewriting: a fixed versioned cue table (`question-intent-v1`) over analyzer terms,
using the same fixed `SparseAnalyzerConfig()` as `question.py` and for the same reason — a refusal
must not change because a lexical index was rebuilt.

## Decision 2 — the refusal requires a conjunction, never a topic word

This is the decision that makes the policy usable rather than destructive. A medical textbook is
full of "treatment", "dose" and "dangerous", and a classifier firing on those would silently make
the corpus unanswerable for the students it exists for.

A refusal requires **personal framing** (a first-person pronoun) **and** a **clinical action or
risk object** aimed at the asker. Personal framing alone is not enough, because asking to be
*informed* is not asking to be *advised*: an epistemic verb ("know", "understand", "remember")
keeps a first-person question academic.

The contrasting pairs are the real specification:

| Proceeds | Refused |
|---|---|
| "What treatments are described for Legionnaires disease?" | "What treatment should I take?" |
| "What is the amoxicillin dose described in the chapter?" | "How much amoxicillin should I take?" |
| "Is sepsis dangerous?" | "Is this dangerous for me?" |
| "When are antibiotics stopped in endocarditis?" | "Should I stop taking this drug?" |
| "What should I know about Gram staining?" | "What should I do about my infection?" |

Second-person "you" is deliberately *not* personal framing: in a question it is far more often
impersonal ("how do you stain this?") than a request for advice.

## Decision 3 — refusal happens before retrieval

The verdict depends only on the question text, so it is evaluated in `AskService.ask` before
anything is searched. Two consequences, both intended: no index is queried and no provider is
called, and the refusal cannot be influenced by whatever the corpus happened to return. An
unclassifiable input fails closed — it is refused, never waved through.

## Decision 4 — `OUT_OF_SCOPE` is a distinct outcome

`AskOutcome` gains `OUT_OF_SCOPE` with reason code `PERSONAL_MEDICAL_ADVICE_REQUESTED`. Reusing
`INSUFFICIENT_EVIDENCE` was rejected as dishonest: the sources are not the reason, and a reader
told "the indexed sources do not support an answer" would reasonably try rephrasing, add a
document, or conclude the corpus is deficient. None of those is true.

The message is fixed and product-controlled. It states what the system is, tells an urgent reader
to seek care, and points to a clinician for the thing it will not do. It does not diagnose,
prescribe or personalize treatment, and a test asserts that.

## Decision 5 — clarification was considered and rejected

`conversation_id` exists, so a clarification round-trip was available. It was rejected because for
an educational corpus the clarified question — "what should I do about *my* Klebsiella infection" —
is still out of scope. Asking first would imply the system could answer given more detail. Refusing
directly is honest.

## Observability

The verdict is logged for every question (the bounded vocabulary only; the question text is not
logged, per the M5 telemetry rule), and the gate records it as a `question_intent`
`EvaluatedSignal` on every decision it makes. The classifier is a pure function of the question, so
the second call cannot disagree with the first. Without this, a misclassification would be
invisible: the reader sees a refusal and cannot tell it was wrong.

## Consequences

Of the 26 gold questions, exactly one changes: A3 becomes `OUT_OF_SCOPE`. The other 25 — including
every supported and context question, and A1 and A2 — proceed unchanged. No cue table was derived
from or tuned against the gold set.

**The main residual risk is over-blocking**, and its failure mode is silent: a student writing
naturally in the first person ("should I use Ziehl-Neelsen or Gram stain here?") sees a refusal and
cannot tell it was a misclassification. The conjunction is the mitigation and the contrasting-pair
tests are how it is held; the logged signal is how a real miss would be found.

**Under-blocking is bounded and accepted.** An impersonal but clinically actionable question —
"what is the amoxicillin dose for otitis media?" — proceeds, and should: it asks what the book
states. The residual risk is a reader treating an educational fact as advice, which is a
product-surface concern rather than a gate concern.
