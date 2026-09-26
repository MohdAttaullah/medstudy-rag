"""Which source figures an answer may show, decided from provenance rather than from proximity.

The rule this module exists to enforce: a figure appears beside an answer only when something the
answer actually cites links to it. Sharing a page is not a link; sharing a document is certainly
not. Two links qualify, and both are checkable by looking:

*The cited evidence is the figure.* A `FIGURE_CONTEXT` citation already carries its artifact id, so
the figure behind it is known exactly.

*The cited text names the figure.* The stored citation text — the words M8 verified against, not a
paraphrase — says "( Figs. 1.2-1.4 )", and a figure in the same parse run declares the caption
"FIGURE 1.2. …". The label matches or it does not; nothing is inferred from the subject matter.

What this is not: it is not evidence. A figure shown here has been interpreted by nothing, supports
no claim, and cannot make an answer more verified than M8 found it. Claims resting on figures are
still refused upstream by `VISUAL_INTERPRETATION_REQUIRED`; this module only decides what may be
displayed beside an answer that already stands on its text.
"""

import re
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.parsing import FigureArtifact
from app.schemas.ask import AskCitation, AskFigure

#: The opening of a figure reference: "Figure", "Fig.", "Figs.", "FIGURE". Deliberately narrow —
#: a bare number, a page reference or a section number must not match, because a wrong match would
#: show a figure the source never pointed at.
# Longest alternative first: "figures" must not match as "figure" and leave the "s" behind, which
# would put a letter between the prefix and the label and stop the scan before it started.
PREFIX = re.compile(r"\bFig(?:ures|ure|s)?\.?\s*", re.IGNORECASE)

#: One label, optionally the low end of a range: "1.2", "1.2-1.4", "3".
LABEL = re.compile(r"(\d+(?:\.\d+)?)(?:\s*[-–—]\s*(\d+(?:\.\d+)?))?")

#: What may join two labels inside one reference: "Figs. 1.2 and 1.7", "Figs. 1.5, 1.6 and 1.8".
SEPARATOR = re.compile(r"\s*,?\s*(?:and\s+|&\s*)?", re.IGNORECASE)

#: A reference naming more than this is not one a reader follows; stop rather than guess.
MAX_LABELS_PER_REFERENCE = 8

#: The label a caption declares, read from its opening: "FIGURE 1.7. Brainstem, petrosal …".
CAPTION_LABEL = re.compile(r"^\s*Fig(?:ures|ure|s)?\.?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

#: A cap, so a passage citing a whole plate cannot fill the answer with thumbnails.
MAX_FIGURES = 6


def referenced_labels(text: str) -> list[str]:
    """Figure labels the text itself names.

    A reference is read the way it is written. "Figs. 1.2-1.4" names three figures and the source
    means all three, so the range is expanded — but only across the minor number, because 1.9 to
    2.1 is not a sequence this can reason about. "Figs. 1.2 and 1.7" names two, and reading only
    the first drops a figure the source pointed at: this corpus introduces the petrosal surface as
    "( Figs. 1.2 and 1.7 )", and 1.7 is the one whose caption can be matched.

    Scanning stops at the first thing that is not another label, so "Fig. 1.2 and the tentorium"
    yields one label and consumes none of the prose after it.
    """
    body = text or ""
    found: list[str] = []
    for prefix in PREFIX.finditer(body):
        position = prefix.end()
        for _ in range(MAX_LABELS_PER_REFERENCE):
            label = LABEL.match(body, position)
            if label is None:
                break
            for value in _expand(label.group(1), label.group(2) or ""):
                if value not in found:
                    found.append(value)
            separator = SEPARATOR.match(body, label.end())
            position = separator.end() if separator else label.end()
            if position == label.end():
                break
    return found


def _expand(first: str, last: str) -> list[str]:
    if not last:
        return [first]
    start, end = first.split("."), last.split(".")
    if len(start) != 2 or len(end) != 2 or start[0] != end[0]:
        # A range across majors, or between differently shaped labels: keep the endpoints only.
        return [first, last]
    low, high = int(start[1]), int(end[1])
    if not 0 <= high - low <= 12:
        return [first, last]
    return [f"{start[0]}.{value}" for value in range(low, high + 1)]


def caption_label(caption: str | None) -> str | None:
    match = CAPTION_LABEL.match(caption or "")
    return match.group(1) if match else None


def resolve(session: Session, tenant_id: UUID, citations: Sequence[AskCitation]) -> list[AskFigure]:
    """The figures the citations link to, in page order. Never more than the citations justify."""
    if not citations:
        return []

    runs = {c.parse_run_id for c in citations}
    rows = session.scalars(
        select(FigureArtifact).where(
            FigureArtifact.tenant_id == tenant_id,
            FigureArtifact.parse_run_id.in_(runs),
            # Only a figure with a stored image can be shown; there is nothing to display without
            # one, and offering a link that 404s is worse than offering none.
            FigureArtifact.image_key.is_not(None),
        )
    ).all()
    if not rows:
        return []

    by_id: dict[UUID, FigureArtifact] = {row.id: row for row in rows}
    by_label: dict[tuple[UUID, str], FigureArtifact] = {}
    for row in rows:
        label = caption_label(row.caption_text)
        if label is not None:
            by_label.setdefault((row.parse_run_id, label), row)

    found: dict[UUID, AskFigure] = {}

    def record(
        row: FigureArtifact,
        citation: AskCitation,
        how: Literal["CITED_EVIDENCE", "CITED_TEXT_REFERENCE"],
        label: str | None,
    ) -> None:
        existing = found.get(row.id)
        if existing is not None:
            if citation.citation_id not in existing.citation_ids:
                found[row.id] = existing.model_copy(
                    update={"citation_ids": [*existing.citation_ids, citation.citation_id]}
                )
            return
        found[row.id] = AskFigure(
            figure_id=row.id,
            document_id=citation.document_id,
            document_version_id=citation.document_version_id,
            parse_run_id=row.parse_run_id,
            document_title=citation.document_title,
            page=row.page_number,
            caption=row.caption_text,
            label=label,
            linked_by=how,
            citation_ids=[citation.citation_id],
        )

    for citation in citations:
        # The citation *is* a figure: the strongest link there is.
        for artifact in citation.artifacts:
            cited = by_id.get(artifact.artifact_id)
            if artifact.kind == "FIGURE" and cited is not None:
                record(cited, citation, "CITED_EVIDENCE", caption_label(cited.caption_text))
        # The citation's own verified words name a figure.
        for label in referenced_labels(citation.cited_text):
            named = by_label.get((citation.parse_run_id, label))
            if named is not None:
                record(named, citation, "CITED_TEXT_REFERENCE", label)

    ordered = sorted(
        found.values(),
        key=lambda figure: (
            figure.page is None,
            figure.page or 0,
            figure.label or "",
            str(figure.figure_id),
        ),
    )
    return ordered[:MAX_FIGURES]
