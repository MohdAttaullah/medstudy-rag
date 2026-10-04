"""The sentence a reader is shown for a turn, and the rule that picks it.

An outcome's sentence used to be chosen from the outcome alone, so a question refused for being too
long read "This is a technical failure". These tests pin the rule that replaced it: a small, closed
set of declared reasons speak for themselves; everything else keeps its outcome's sentence — and a
real outage keeps the technical one.
"""

from app.retrieval.errors import MESSAGES as RETRIEVAL_CODES
from app.services.ask import (
    MESSAGES,
    NOTHING_TO_SEARCH,
    PROVIDER_FAILURES,
    REASON_MESSAGES,
    message,
)


def test_every_reason_with_its_own_sentence_is_a_code_the_pipeline_really_raises():
    """A misspelt key would never match, and the old wording would come back without a sound."""
    assert set(REASON_MESSAGES) <= set(RETRIEVAL_CODES)


def test_no_reason_sentence_claims_the_service_broke():
    for code, sentence in REASON_MESSAGES.items():
        assert "technical failure" not in sentence.lower(), code
        assert sentence != MESSAGES["FAILED"], code


def test_an_over_long_question_is_told_to_shorten_and_why_it_was_not_truncated():
    sentence = message("FAILED", ["QUERY_TOO_LONG"])
    assert "more briefly" in sentence
    assert "not shortened automatically" in sentence
    # It must not suggest simply trying again: the same question fails the same way.
    assert "try again" not in sentence.lower()


def test_an_empty_corpus_says_nothing_was_filled_in_and_names_no_document():
    sentence = message("INSUFFICIENT_EVIDENCE", ["RETRIEVAL_CORPUS_EMPTY"])
    assert "Nothing has been filled in from the model's own knowledge" in sentence
    # It speaks about the workspace as a whole; it cannot know which document mattered.
    assert "No document in this workspace is ready" in sentence


def test_an_empty_corpus_is_the_only_declared_code_that_becomes_an_abstention():
    assert NOTHING_TO_SEARCH == frozenset({"RETRIEVAL_CORPUS_EMPTY"})


def test_every_provider_failure_keeps_the_technical_sentence():
    for code in PROVIDER_FAILURES:
        assert message("FAILED", [code]) == MESSAGES["FAILED"], code
    assert "technical failure" in MESSAGES["FAILED"]


def test_infrastructure_codes_outside_the_closed_set_keep_the_technical_sentence():
    for code in ("QUERY_ENCODER_UNAVAILABLE", "DENSE_SEARCH_FAILED", "RETRIEVAL_CORPUS_MISALIGNED"):
        assert message("FAILED", [code]) == MESSAGES["FAILED"], code


def test_an_unknown_or_absent_reason_falls_back_to_the_outcome():
    assert message("FAILED", ["SOMETHING_NEW"]) == MESSAGES["FAILED"]
    assert message("INSUFFICIENT_EVIDENCE", None) == MESSAGES["INSUFFICIENT_EVIDENCE"]
    assert message("VERIFIED", []) == MESSAGES["VERIFIED"]


def test_ordinary_abstentions_are_unchanged():
    """The gate's own codes never had a problem and must read exactly as before."""
    assert (
        message("INSUFFICIENT_EVIDENCE", ["ASSESSMENT_ONLY_EVIDENCE", "ADVISORY_CONTEXT_OMISSION"])
        == MESSAGES["INSUFFICIENT_EVIDENCE"]
    )
