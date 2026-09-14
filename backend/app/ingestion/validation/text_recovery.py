"""Whether a page's source text survived into the parsed document, wherever it was anchored.

A page-level character count cannot tell two very different things apart:

  A. the parser dropped this page's text, and it is gone;
  B. the parser kept the text and anchored it to the page the paragraph *started* on.

Both look identical from page N — few parsed characters, many source characters — and the second is
correct behaviour. On a real 932-page textbook the char rule reported two ERRORs and **both** were
case B or page furniture: page 517 carries the tail of a paragraph beginning on 516, and page 44 is
a section divider whose decorative "S E C T I O N" lettering does not survive tokenization while
its actual title does.

So the question is asked directly: is this page's source text present in the parsed document? The
comparison is over **material words** — tokens of two characters or more, casefolded, as a set.

That unit is chosen deliberately. A character ratio cannot express "present but elsewhere" at all.
Word n-grams looked promising and were measured, but they scored page 44 at 0.20 purely because
letter-spaced display text shingles into fragments like `c t i o n`, which is a property of the
extractor rather than of the document. Set-of-words comparison is insensitive to re-flowed
whitespace, to reading-order differences between the text layer and the parser, and to the
duplication a cross-page join produces — and on the two real cases it scores 1.000 for both, well
clear of ordinary pages at 0.80-0.99.

Nothing here decides severity. It measures; `parse_quality` decides.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

#: Two characters or more: single letters carry no identity and are exactly what decorative
#: letter-spacing shatters real words into.
WORD = re.compile(r"[^\W_][\w'-]*", re.UNICODE)
MIN_WORD = 2

#: How far a cross-page paragraph can reasonably have been anchored. One page either side covers
#: the paragraph-start anchoring this exists for; widening it would start excusing real loss.
NEIGHBOURHOOD = 1


def material_words(text: str) -> set[str]:
    """The distinct words of `text` that carry identity."""
    return {word.casefold() for word in WORD.findall(text or "") if len(word) >= MIN_WORD}


def coverage(source: str, parsed: str) -> float:
    """Fraction of the source's material words present in `parsed`.

    A source with no material words is fully covered by definition: there is nothing to lose.
    """
    wanted = material_words(source)
    if not wanted:
        return 1.0
    return len(wanted & material_words(parsed)) / len(wanted)


def page_source_text(path: Path, page_numbers: set[int]) -> dict[int, str]:
    """Source-layer text for the named 1-based pages only.

    Only the named pages, and the caller names only pages already flagged as suspect. Extracting a
    whole book at once is not bounded memory — pypdf retains each page's decompressed content
    stream, measured at ~1.3 GiB on the 932-page textbook — and this runs after conversion, when
    that headroom is least available.
    """
    if not page_numbers:
        return {}
    from pypdf import PdfReader

    found: dict[int, str] = {}
    logging.disable(logging.ERROR)
    try:
        reader = PdfReader(str(path))
        total = len(reader.pages)
        for number in sorted(page_numbers):
            if not 1 <= number <= total:
                continue
            try:
                found[number] = (reader.pages[number - 1].extract_text() or "").strip()
            except Exception:
                # An unreadable page yields no source text, so nothing can be shown recovered
                # from it and the page keeps whatever severity the character rule gave it.
                found[number] = ""
    except Exception:
        return {}
    finally:
        logging.disable(logging.NOTSET)
    return found


@dataclass(frozen=True)
class PageRecovery:
    """Where a suspect page's source text turned up, if anywhere."""

    #: Covered by this page's own parsed text. High here means the shortfall was formatting —
    #: decorative letter-spacing, running heads — rather than anything missing.
    own: float
    #: Covered by this page together with its immediate neighbours. High here while `own` is low
    #: is the signature of a cross-page paragraph anchored to the page it begins on.
    neighbourhood: float
    #: The pages compared against, so a reviewer knows where to look.
    pages: tuple[int, ...]


def recovery(
    path: Path,
    suspect: set[int],
    parsed_by_page: dict[int, str],
    neighbourhood: int = NEIGHBOURHOOD,
) -> dict[int, PageRecovery]:
    """Per suspect page, where its source text was found.

    Deterministic: the same parse and the same file always produce the same numbers.
    """
    sources = page_source_text(path, suspect)
    found: dict[int, PageRecovery] = {}
    for number, source in sources.items():
        span = tuple(range(number - neighbourhood, number + neighbourhood + 1))
        nearby = "\n".join(parsed_by_page.get(n, "") for n in span)
        found[number] = PageRecovery(
            own=coverage(source, parsed_by_page.get(number, "")),
            neighbourhood=coverage(source, nearby),
            pages=tuple(n for n in span if n in parsed_by_page),
        )
    return found
