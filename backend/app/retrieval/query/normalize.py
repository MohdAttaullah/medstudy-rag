"""Deterministic query preparation.

Every transformation here is reversible in intent and mechanical in nature: Unicode form, control
characters, typographic punctuation and whitespace. Nothing is added, removed, reworded or
interpreted.

What is deliberately **not** here, and must not be added without its own ADR and evaluation:

* LLM query rewriting and HyDE — a generated pseudo-answer is model memory, and retrieving against
  it lets pretrained belief steer which evidence is found;
* paraphrasing or alternate query generation — the question the clinician asked is the question
  that must be answered;
* synonym and abbreviation expansion — `MI` may be myocardial infarction or mitral incompetence,
  and an uncontrolled expansion changes what was asked. M5 measures plain retrieval first.

Clinically meaningful characters survive: `+`, `-`, `/`, `%`, degrees, micro, and the comparison
symbols that appear in dosing and laboratory ranges.
"""

import hashlib
import unicodedata

from app.core.retrieval_config import QueryEncoderConfig

# Typographic characters mapped to the ASCII forms a corpus and a keyboard agree on. This is the
# same folding the lexical analyzer applies, so both lanes see the same question.
PUNCTUATION = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    " ": " ",
    "\t": " ",
    "\r": " ",
    "\n": " ",
    "⁄": "/",
}


def normalize(text: str, config: QueryEncoderConfig) -> str:
    """The exact text that will be encoded and analyzed."""
    folded = "".join(PUNCTUATION.get(character, character) for character in text)
    # Control characters carry no meaning in a typed question and can confuse a tokenizer.
    _ = config  # The steps are fixed by `normalization_version`; the config selects the version.
    stripped = "".join(
        character
        for character in unicodedata.normalize("NFKC", folded)
        if unicodedata.category(character)[0] != "C"
    )
    return " ".join(stripped.split())


def query_hash(text: str, config: QueryEncoderConfig) -> str:
    """Identity of a normalized question under one normalization and encoder version.

    Traces and metrics carry this rather than the question itself, so a retrieval can be
    correlated and reproduced without medical queries accumulating in logs.
    """
    payload = "\x1f".join(
        (config.normalization_version, config.model_id, config.model_revision, text)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
