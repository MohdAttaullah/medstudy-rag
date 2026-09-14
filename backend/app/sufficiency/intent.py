"""Deterministic medical-intent classification.

This is orthogonal to `question.py`. That module asks *what shape of evidence* a question needs;
this one asks *what the reader is asking the system to do*. A question can be ORDINARY_FACTUAL and
still be a request for personal medical advice, which is why one axis could not carry both.

The system is an educational medical evidence workspace, not a clinical decision system. It has no
patient, no history, no examination and no prescriber, so an individualized clinical request is not
a question it answers badly — it is a question it must not answer at all. Before this module the
only thing standing against one was a sentence in the provider prompt, which is a probabilistic
instruction on the untrusted side of the boundary. See ADR-021.

No model, no rewriting, no paraphrase: a fixed versioned cue table over the reader's own words.

The rule that keeps it from eating the corpus is a **conjunction**. A topic word is never enough —
"treatment", "dose" and "dangerous" are ordinary vocabulary in a medical textbook, and a question
containing one is usually a student asking what the book says. A refusal requires personal framing
*and* a clinical action or risk directed at the asker.
"""

from typing import Literal

from app.core.retrieval_config import SparseAnalyzerConfig
from app.retrieval.sparse.analyzer import EXACT_PREFIX, terms

CLASSIFIER_VERSION: Literal["question-intent-v1"] = "question-intent-v1"

QuestionIntent = Literal[
    # Proceeds: what the system exists to answer.
    "ACADEMIC_FACTUAL",
    # Proceeds: asks what the corpus describes about treatment, not what the reader should do.
    "EDUCATIONAL_TREATMENT",
    # Refused: the reader is asking to be told what to do.
    "PERSONAL_ACTION_SEEKING",
    "INDIVIDUALIZED_TREATMENT",
    "PERSONAL_DOSAGE",
    "MEDICATION_CHANGE",
    "PERSONAL_URGENT_RISK",
]

#: Intents that never reach retrieval. Everything else proceeds.
REFUSED: frozenset[str] = frozenset(
    {
        "PERSONAL_ACTION_SEEKING",
        "INDIVIDUALIZED_TREATMENT",
        "PERSONAL_DOSAGE",
        "MEDICATION_CHANGE",
        "PERSONAL_URGENT_RISK",
    }
)

# A fixed tokenization, deliberately not the tenant's active index analyzer: a refusal must not
# change because a lexical index was rebuilt. Same reasoning, and same constant, as `question.py`.
ANALYZER = SparseAnalyzerConfig()

# --- The first half of the conjunction: is the asker in the question? -------------------------

#: First-person reference. "my" covers "my infection"; "we"/"our" covers a reader asking on behalf
#: of themselves and another. Second person is deliberately absent: "you" in a question is far more
#: often impersonal ("how do you stain this?") than a request for personal advice.
PERSONAL_PRONOUNS = frozenset({"i", "me", "my", "mine", "myself", "we", "us", "our", "ours"})

# --- The second half: is a clinical action or risk being sought? ------------------------------

#: Asking to be informed is not asking to be advised. "What should I know about Gram staining?" is
#: a study question, and personal framing alone must never refuse it.
EPISTEMIC_VERBS = frozenset(
    {
        "know",
        "learn",
        "understand",
        "study",
        "read",
        "revise",
        "remember",
        "memorize",
        "memorise",
        "expect",
        "call",
        "name",
        "mean",
        "means",
        "say",
        "says",
    }
)

#: Taking or being given something.
INTAKE_VERBS = frozenset({"take", "taking", "took", "use", "using", "given", "receive", "swallow"})

#: Being prescribed or needing something.
PRESCRIPTION_VERBS = frozenset({"prescribe", "prescribed", "need", "needs", "get", "buy"})

#: Starting, stopping or altering an existing medication.
CHANGE_VERBS = frozenset(
    {
        "stop",
        "stopping",
        "start",
        "starting",
        "quit",
        "switch",
        "switching",
        "continue",
        "discontinue",
        "change",
        "changing",
        "increase",
        "decrease",
        "reduce",
        "double",
        "skip",
    }
)

#: Generic "do something about it" verbs.
ACTION_VERBS = frozenset(
    {"do", "treat", "manage", "handle", "cure", "fix", "help", "see", "visit", "go"}
)

#: Things a treatment request is about.
THERAPY_NOUNS = frozenset(
    {
        "treatment",
        "treatments",
        "therapy",
        "therapies",
        "antibiotic",
        "antibiotics",
        "drug",
        "drugs",
        "medication",
        "medications",
        "medicine",
        "medicines",
        "tablet",
        "tablets",
        "pill",
        "pills",
        "dose",
        "doses",
        "dosage",
        "injection",
        "vaccine",
        "antiviral",
        "antifungal",
        "painkiller",
        "prescription",
        "remedy",
        "supplement",
    }
)

#: Quantity framing, which is what makes a dosage question a dosage question.
DOSAGE_CUES = frozenset(
    {"much", "many", "mg", "ml", "gram", "grams", "milligram", "milligrams", "tablets", "often"}
)

#: Danger directed at a person.
RISK_NOUNS = frozenset(
    {
        "dangerous",
        "danger",
        "serious",
        "harmful",
        "harm",
        "risky",
        "risk",
        "fatal",
        "deadly",
        "lethal",
        "emergency",
        "urgent",
        "safe",
        "safety",
        "worried",
        "worry",
        "worrying",
        "contagious",
        "infectious",
        "die",
        "dying",
        "kill",
    }
)

#: A condition the reader may be describing as their own.
CONDITION_NOUNS = frozenset(
    {
        "infection",
        "infections",
        "illness",
        "disease",
        "symptom",
        "symptoms",
        "fever",
        "rash",
        "pain",
        "cough",
        "wound",
        "condition",
        "sepsis",
        "abscess",
        "sore",
        "lump",
        "bite",
    }
)


class IntentError(Exception):
    """Raised when the question cannot be classified. The caller must fail closed."""


def _words(question: str) -> set[str]:
    """Casefolded analyzer terms with the case-exact marker removed.

    `terms` emits a `^`-prefixed variant for any token carrying uppercase, so a sentence-initial
    "Should" arrives as both `should` and `^Should`. Matching cue tables against the marked form
    would make a rule depend on capitalization, and "should i" at the start of a sentence would
    then behave differently from the same words mid-sentence.
    """
    found = set()
    for term in terms(question, ANALYZER):
        bare = term[len(EXACT_PREFIX) :] if term.startswith(EXACT_PREFIX) else term
        bare = bare.casefold()
        if bare:
            found.add(bare)
    return found


def classify(question: str) -> QuestionIntent:
    """What the reader is asking the system to do.

    Deterministic and total: every question receives exactly one intent, and an input this module
    cannot tokenize raises rather than defaulting to the permissive answer.
    """
    if not isinstance(question, str):
        raise IntentError("A question must be text.")
    try:
        asked = _words(question)
    except Exception as exc:  # pragma: no cover - defensive; the analyzer is pure
        raise IntentError("The question could not be tokenized.") from exc

    personal = bool(asked & PERSONAL_PRONOUNS)
    therapy = bool(asked & THERAPY_NOUNS)

    if personal and not (asked & EPISTEMIC_VERBS):
        # Order is most specific first: a dosage question is also a treatment question, and the
        # narrower name is the more useful one in a decision record.
        if asked & DOSAGE_CUES and (asked & INTAKE_VERBS or therapy):
            return "PERSONAL_DOSAGE"
        if asked & CHANGE_VERBS and (therapy or asked & INTAKE_VERBS):
            return "MEDICATION_CHANGE"
        if therapy and (asked & INTAKE_VERBS or asked & PRESCRIPTION_VERBS):
            return "INDIVIDUALIZED_TREATMENT"
        if asked & RISK_NOUNS:
            return "PERSONAL_URGENT_RISK"
        if asked & ACTION_VERBS and (therapy or asked & CONDITION_NOUNS):
            return "PERSONAL_ACTION_SEEKING"

    if therapy:
        return "EDUCATIONAL_TREATMENT"
    return "ACADEMIC_FACTUAL"


def permitted(intent: QuestionIntent) -> bool:
    """Whether a question with this intent may proceed to retrieval."""
    return intent not in REFUSED
