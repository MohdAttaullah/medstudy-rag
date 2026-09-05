"""Deterministic text normalization.

Every transformation here is content-preserving: it repairs artefacts introduced by PDF text
extraction, never the document's meaning. Numbers, units, doses, drug names, abbreviations and
formulas are left exactly as the parser reported them. Nothing in this module may "correct" a
value because it looks clinically implausible - source fidelity outranks plausibility at parse
time, and a genuine source error must stay visible downstream.

The original parser text is persisted alongside the normalized text whenever the two differ, so
any normalization remains auditable against the source.
"""

import re
import unicodedata

SOFT_HYPHEN = "­"
LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}
# Zero-width marks and the byte order mark carry no textual content after extraction.
ZERO_WIDTH = "​‌‍⁠﻿"
# A word split across an extracted line break, for the common hyphen code points.
HYPHEN_LINEBREAK = re.compile(r"(\w)[‐‑-]\n(\w)")
SINGLE_NEWLINE = re.compile(r"(?<!\n)\n(?!\n)")
# Horizontal whitespace, including non-breaking, thin and ideographic spaces.
SPACES = re.compile(r"[ \t   -   　]+")
BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(value: str | None) -> str:
    """Apply the allowed deterministic repairs; returns "" only for genuinely empty input."""
    if not value:
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(SOFT_HYPHEN, "")
    for ligature, replacement in LIGATURES.items():
        text = text.replace(ligature, replacement)
    text = text.translate({ord(char): None for char in ZERO_WIDTH})
    text = unicodedata.normalize("NFKC", text)
    # Repair a hyphenated word split before collapsing the line break that split it.
    text = HYPHEN_LINEBREAK.sub(r"\1\2", text)
    text = SINGLE_NEWLINE.sub(" ", text)
    text = SPACES.sub(" ", text)
    text = BLANK_LINES.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def normalize_expression(value: str | None) -> str | None:
    """Whitespace-only normalization for formulas.

    Symbols, operators, sub/superscripts and intra-token spacing are untouched: a formula is
    evidence, and no deterministic rewrite of mathematical notation is safe enough to apply.
    """
    if value is None:
        return None
    collapsed = SPACES.sub(" ", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    return collapsed or None


def changed(original: str | None, normalized: str | None) -> bool:
    return (original or "") != (normalized or "")


def page_text(fragments: list[str]) -> str:
    """Join already-normalized element text into a stable per-page rendering."""
    return "\n\n".join(fragment for fragment in fragments if fragment)
