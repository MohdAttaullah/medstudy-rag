"""Conservative source-pattern grouping. No answer inference or medical correction."""

import hashlib
import re

from app.ingestion.chunking.model import QuestionDraft, QuestionOptionDraft, SourceElement, Span

QUESTION = re.compile(r"(?m)^(?:Question\s+)?(\d{1,4})[.)]\s+", re.I)
OPTION = re.compile(r"(?<!\w)([A-H])[.)]\s+")
ANSWER = re.compile(r"\b(?:Correct\s+answer|Answer)\s*:\s*", re.I)
EXPLANATION = re.compile(r"\bExplanation\s*:\s*", re.I)


def ranges(elements: list[SourceElement]) -> tuple[str, list[tuple[int, int, SourceElement]]]:
    text = ""
    offsets = []
    for element in elements:
        if text:
            text += "\n"
        offsets.append((len(text), len(text) + len(element.text), element))
        text += element.text
    return text, offsets


def source_spans(
    offsets: list[tuple[int, int, SourceElement]], start: int, end: int
) -> tuple[Span, ...]:
    return tuple(
        Span(element_id=e.id, start=max(start, a) - a, end=min(end, b) - a)
        for a, b, e in offsets
        if min(end, b) > max(start, a)
    )


def extract(
    elements: list[SourceElement], authority: dict[str, object]
) -> list[tuple[QuestionDraft, tuple[Span, ...], tuple[Span, ...]]]:
    text, offsets = ranges(elements)
    starts = list(QUESTION.finditer(text))
    result = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        body = text[match.end() : end]
        explanation = EXPLANATION.search(body)
        answer = ANSWER.search(body)
        option_end = min([m.start() for m in (answer, explanation) if m] or [len(body)])
        matches = list(OPTION.finditer(body[:option_end]))
        stem_end = matches[0].start() if matches else option_end
        options = tuple(
            QuestionOptionDraft(
                label=m.group(1),
                text=body[
                    m.end() : matches[i + 1].start() if i + 1 < len(matches) else option_end
                ].strip(),
                ordinal=i,
            )
            for i, m in enumerate(matches)
        )
        labels = [o.label for o in options]
        valid = (not labels or labels == list("ABCDEFGH"[: len(labels)])) and all(
            o.text for o in options
        )
        kind = "MCQ" if len(options) >= 2 else "UNKNOWN"
        stem = body[:stem_end].strip()
        if re.search(r"\bselect all\b|\bmultiple (?:answers|selections)\b", stem, re.I) and options:
            kind = "MULTI_SELECT"
        elif re.search(r"\btrue\s*(?:or|/)\s*false\b", stem, re.I):
            kind = "TRUE_FALSE"
        elif re.search(r"\bshort answer\b", stem, re.I):
            kind = "SHORT_ANSWER"
        elif re.search(r"\blong answer\b|\bessay\b", stem, re.I):
            kind = "LONG_ANSWER"
        explicit = None
        if answer and (not explanation or answer.start() < explanation.start()):
            explicit = (
                body[answer.end() : explanation.start() if explanation else len(body)].strip()
                or None
            )
        exp = body[explanation.end() :].strip() if explanation else None
        all_spans = source_spans(offsets, match.start(), end)
        exp_start = match.end() + explanation.start() if explanation else end
        key = hashlib.sha256((str(all_spans) + text[match.start() : end]).encode()).hexdigest()
        draft = QuestionDraft(
            key=key,
            number=match.group(1),
            text=stem,
            question_type=kind,
            options=options,
            explicit_answer=explicit,
            explanation=exp,
            extraction_status="EXPLICIT_PATTERN" if valid and stem else "NEEDS_REVIEW",
            source_spans=all_spans,
            authority=authority,
        )
        result.append(
            (
                draft,
                source_spans(offsets, match.start(), exp_start),
                source_spans(offsets, exp_start, end),
            )
        )
    return result
