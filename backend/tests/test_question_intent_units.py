"""What the reader is asking the system to do, decided before anything is retrieved.

The system is an educational evidence workspace with no patient, no history and no prescriber, so
an individualized clinical request is not a question it answers badly -- it is one it must not
answer at all. Before this classifier the only thing standing against one was a sentence in the
provider prompt.

The hard part is not refusing; it is refusing *only* the right questions. A medical textbook is
full of the words "treatment", "dose" and "dangerous", and a classifier that fired on those would
silently make the corpus unusable for the students it exists for. Every refusal therefore needs
personal framing **and** a clinical action or risk aimed at the asker, and the contrasting pairs
below are the real test of that conjunction. See ADR-021.
"""

import pytest
from app.sufficiency.intent import CLASSIFIER_VERSION, IntentError, classify, permitted

# --------------------------------------------------------- the seven declared categories


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What is dysbiosis?", "ACADEMIC_FACTUAL"),
        ("What treatments are described for Legionnaires disease?", "EDUCATIONAL_TREATMENT"),
        ("What should I do about the infection?", "PERSONAL_ACTION_SEEKING"),
        ("What antibiotic should I take?", "INDIVIDUALIZED_TREATMENT"),
        ("How much amoxicillin should I take?", "PERSONAL_DOSAGE"),
        ("Should I stop taking this drug?", "MEDICATION_CHANGE"),
        ("Is this dangerous for me?", "PERSONAL_URGENT_RISK"),
    ],
)
def test_every_declared_category_is_reachable(question, expected):
    assert classify(question) == expected


def test_only_the_personal_categories_are_refused():
    assert permitted("ACADEMIC_FACTUAL")
    assert permitted("EDUCATIONAL_TREATMENT")
    for refused in (
        "PERSONAL_ACTION_SEEKING",
        "INDIVIDUALIZED_TREATMENT",
        "PERSONAL_DOSAGE",
        "MEDICATION_CHANGE",
        "PERSONAL_URGENT_RISK",
    ):
        assert not permitted(refused)


# ------------------------------------------------------------------- contrasting pairs
#
# Each pair differs only in whether the reader is asking about the corpus or about themselves.
# A classifier keying on the topic word alone would get both halves wrong in the same direction.


@pytest.mark.parametrize(
    "educational,personal",
    [
        (
            "What treatments are described for Legionnaires disease?",
            "What treatment should I take?",
        ),
        (
            "What is the amoxicillin dose described in the chapter?",
            "How much amoxicillin should I take?",
        ),
        ("What is the recommended dose described in this textbook?", "What dose should I take?"),
        ("Is sepsis dangerous?", "Is this dangerous for me?"),
        ("When are antibiotics stopped in endocarditis?", "Should I stop taking this drug?"),
        ("How is a Klebsiella infection treated?", "What should I do about my infection?"),
    ],
)
def test_the_corpus_question_proceeds_and_the_personal_one_does_not(educational, personal):
    assert permitted(classify(educational)), educational
    assert not permitted(classify(personal)), personal


def test_first_person_alone_never_refuses():
    """ "What should I know about X" is a study question, not a request to be advised."""
    for question in (
        "What should I know about Gram staining?",
        "What should I remember about the Ziehl-Neelsen stain?",
        "What do I call the paracortex of a lymph node?",
        "What should I understand about complement activation?",
    ):
        assert permitted(classify(question)), question


@pytest.mark.parametrize(
    "question",
    [
        "What is the best treatment?",
        "Is it dangerous?",
        "What are the treatment options for tuberculosis?",
        "Which antibiotic is most active against Pseudomonas?",
        "What dose of gentamicin does the table give?",
        "How dangerous is untreated meningitis?",
        "Describe the treatment of Legionnaires disease.",
    ],
)
def test_a_topic_word_is_never_enough_on_its_own(question):
    """The words treatment, dose and dangerous are ordinary textbook vocabulary."""
    assert permitted(classify(question)), question


@pytest.mark.parametrize(
    "question",
    [
        "What antibiotic should I take for my chest infection?",
        "How much paracetamol can I take?",
        "Should I stop my antibiotics early?",
        "Do I need antibiotics for this?",
        "Is my rash serious?",
        "What should I do about my fever?",
        "Should I start taking this medication?",
    ],
)
def test_a_personal_clinical_request_is_refused(question):
    assert not permitted(classify(question)), question


# ------------------------------------------------------------------------ contract


def test_the_classifier_is_deterministic():
    question = "What should I do about the infection?"
    assert len({classify(question) for _ in range(25)}) == 1


def test_the_classifier_version_is_pinned():
    assert CLASSIFIER_VERSION == "question-intent-v1"


def test_classification_is_total():
    """Every question gets exactly one intent, including nonsense and emptiness."""
    for question in ("asdkjh qwerty ?? zzz plok", "?", "   ", "a"):
        assert classify(question) in {
            "ACADEMIC_FACTUAL",
            "EDUCATIONAL_TREATMENT",
            "PERSONAL_ACTION_SEEKING",
            "INDIVIDUALIZED_TREATMENT",
            "PERSONAL_DOSAGE",
            "MEDICATION_CHANGE",
            "PERSONAL_URGENT_RISK",
        }


def test_a_non_text_question_raises_rather_than_being_waved_through():
    """The caller fails closed on this; returning a permissive default here would hide it."""
    for bad in (None, 42, ["question"]):
        with pytest.raises(IntentError):
            classify(bad)  # type: ignore[arg-type]


def test_capitalization_does_not_change_the_verdict():
    """The analyzer emits a case-marked variant; a rule must not depend on which one it sees."""
    for question in ("should i stop taking this drug?", "SHOULD I STOP TAKING THIS DRUG?"):
        assert classify(question) == "MEDICATION_CHANGE"
