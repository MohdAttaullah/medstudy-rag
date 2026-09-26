"""Which figures an answer may show, and — more importantly — which it may not.

The failure this guards against is the tempting one: a figure on the same page as a citation looks
related, so showing it feels helpful. It is not. Nothing read that image, nothing verified it, and
putting it beside an answer implies the answer rests on it. A figure appears only when the answer's
own cited evidence links to it — the citation *is* that figure, or the citation's verified text
names it by label.
"""

from uuid import uuid4

import pytest
from app.schemas.ask import AskCitation, AskFigure
from app.services.figures import caption_label, referenced_labels, resolve


def citation(text: str, *, artifacts=(), parse_run=None, pages=(1,)) -> AskCitation:
    return AskCitation(
        citation_id=uuid4(),
        ordinal=1,
        document_id=uuid4(),
        document_version_id=uuid4(),
        parse_run_id=parse_run or uuid4(),
        chunk_run_id=uuid4(),
        document_title="Cerebellum and Fourth Ventricle",
        source_type="TEXTBOOK",
        authority_level="REFERENCE",
        chunk_type="TEXT_CHILD",
        pages=list(pages),
        spans=[],
        artifacts=list(artifacts),
        cited_text=text,
    )


class Figure:
    """The columns `resolve` reads from a FigureArtifact row."""

    def __init__(self, caption, page, parse_run, image=True):
        self.id = uuid4()
        self.caption_text = caption
        self.page_number = page
        self.parse_run_id = parse_run
        self.image_key = "documents/x/figure.png" if image else None


class Session:
    """A session that answers the one query `resolve` makes, honouring its filters."""

    def __init__(self, rows):
        self.rows = rows
        self.queried = 0

    def scalars(self, statement):
        self.queried += 1
        # The real query filters on tenant, parse run and a present image; the rows handed in are
        # already the tenant's, so the image filter is what is reproduced here.
        return _Result([row for row in self.rows if row.image_key is not None])


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


# ------------------------------------------------------------------ label reading


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The tentorial surface faces the tentorium ( Fig. 1.2 ).", ["1.2"]),
        ("as shown in Figure 1.7 and described below", ["1.7"]),
        ("( Figs. 1.2-1.4 )", ["1.2", "1.3", "1.4"]),
        ("FIGURE 1.7. Brainstem, petrosal surface", ["1.7"]),
        ("see figures 2.1–2.3 for the approach", ["2.1", "2.2", "2.3"]),
        ("Fig. 3 shows the exposure", ["3"]),
        # Compound references. The corpus writes the petrosal surface as "( Figs. 1.2 and 1.7 )",
        # and reading only the first label drops the figure that has a caption to match.
        ("The petrosal surface faces the petrous bones ( Figs. 1.2 and 1.7 )", ["1.2", "1.7"]),
        ("( Figs. 1.5, 1.6 and 1.8 )", ["1.5", "1.6", "1.8"]),
        ("( Figs. 1.8 and 1.9 )", ["1.8", "1.9"]),
        # Scanning stops at the first thing that is not a label, taking none of the prose with it.
        ("Fig. 1.2 and the tentorium below it", ["1.2"]),
    ],
)
def test_a_reference_the_source_makes_is_read_exactly(text, expected):
    assert referenced_labels(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "The cerebellum has 1.2 million neurons.",
        "Section 1.2 describes the approach.",
        "page 1.2 of the atlas",
        "The transition is smooth and unmarked.",
        "",
    ],
)
def test_a_number_that_is_not_a_figure_reference_matches_nothing(text):
    assert referenced_labels(text) == []


def test_a_compound_reference_links_every_figure_it_names():
    """Both labels are named by the source, so both are offered — if both can be matched."""
    run = uuid4()
    rows = [
        Figure("FIGURE 1.2. Tentorial, suboccipital and petrosal surfaces.", 2, run),
        Figure("FIGURE 1.7. Brainstem, petrosal surface, and cerebellopontine fissure.", 9, run),
        Figure("FIGURE 1.9. Posterior views of the tela choroidea.", 13, run),
    ]
    found = resolve(
        Session(rows),
        uuid4(),
        [
            citation(
                "The petrosal or anterior surface faces the posterior surface of the petrous bones "
                "( Figs. 1.2 and 1.7 ).",
                parse_run=run,
            )
        ],
    )
    assert [figure.label for figure in found] == ["1.2", "1.7"]
    # The third figure was never named, so it is not offered.
    assert "1.9" not in [figure.label for figure in found]


def test_a_range_across_majors_keeps_its_endpoints_rather_than_inventing_a_sequence():
    # 1.9 to 2.1 is not a range this can walk, so it claims only what was written.
    assert referenced_labels("( Figs. 1.9-2.1 )") == ["1.9", "2.1"]


def test_a_caption_declares_its_own_label():
    assert caption_label("FIGURE 1.7. Brainstem, petrosal surface.") == "1.7"
    assert caption_label("Fig. 3 Cerebellar surfaces") == "3"
    assert caption_label("A photograph of the specimen") is None
    assert caption_label(None) is None


# ------------------------------------------------------------------ selection


def test_a_figure_named_by_the_cited_text_is_shown():
    run = uuid4()
    rows = [Figure("FIGURE 1.2. Tentorial, suboccipital and petrosal surfaces.", 2, run)]
    found = resolve(
        Session(rows),
        uuid4(),
        [citation("The tentorial surface faces the tentorium ( Fig. 1.2 ).", parse_run=run)],
    )
    assert [figure.label for figure in found] == ["1.2"]
    assert found[0].linked_by == "CITED_TEXT_REFERENCE"
    assert found[0].page == 2
    assert found[0].caption.startswith("FIGURE 1.2.")


def test_a_figure_the_answer_never_referenced_is_not_shown():
    """The whole point. Same document, same page, no link — so no thumbnail."""
    run = uuid4()
    rows = [
        Figure("FIGURE 1.2. Tentorial surfaces.", 2, run),
        Figure("FIGURE 1.9. An unrelated dissection.", 2, run),
    ]
    found = resolve(
        Session(rows),
        uuid4(),
        [citation("The tentorial surface faces the tentorium ( Fig. 1.2 ).", parse_run=run)],
    )
    assert [figure.label for figure in found] == ["1.2"]


def test_a_cited_figure_chunk_links_its_own_artifact():
    run = uuid4()
    row = Figure("FIGURE 1.7. Brainstem and petrosal surface.", 9, run)
    cited = citation(
        "FIGURE 1.7. Brainstem and petrosal surface.",
        artifacts=[{"artifact_id": row.id, "kind": "FIGURE", "image_available": True}],
        parse_run=run,
    )
    found = resolve(Session([row]), uuid4(), [cited])
    assert len(found) == 1 and found[0].figure_id == row.id
    assert found[0].linked_by == "CITED_EVIDENCE"


def test_a_table_artifact_on_a_citation_links_no_figure():
    run = uuid4()
    row = Figure("FIGURE 1.2. Surfaces.", 2, run)
    cited = citation(
        "Compound A | 12 h",
        artifacts=[{"artifact_id": row.id, "kind": "TABLE", "image_available": False}],
        parse_run=run,
    )
    assert resolve(Session([row]), uuid4(), [cited]) == []


def test_a_figure_with_no_stored_image_is_never_offered():
    """There is nothing to show, and a link that 404s is worse than no link."""
    run = uuid4()
    rows = [Figure("FIGURE 1.2. Surfaces.", 2, run, image=False)]
    assert resolve(Session(rows), uuid4(), [citation("( Fig. 1.2 )", parse_run=run)]) == []


def test_a_figure_from_a_different_parse_run_is_not_matched():
    """Labels repeat across documents; a link is only a link inside the run that was cited."""
    mine, other = uuid4(), uuid4()
    rows = [Figure("FIGURE 1.2. Someone else's figure.", 2, other)]
    assert resolve(Session(rows), uuid4(), [citation("( Fig. 1.2 )", parse_run=mine)]) == []


def test_an_untitled_figure_is_reachable_only_through_a_cited_artifact():
    """CHUNK_FIGURE_NO_TEXT figures have an image and no caption to match a label against."""
    run = uuid4()
    row = Figure(None, 3, run)
    assert resolve(Session([row]), uuid4(), [citation("( Fig. 1.2 )", parse_run=run)]) == []

    cited = citation(
        "",
        artifacts=[{"artifact_id": row.id, "kind": "FIGURE", "image_available": True}],
        parse_run=run,
    )
    found = resolve(Session([row]), uuid4(), [cited])
    assert len(found) == 1 and found[0].caption is None and found[0].label is None


def test_one_figure_referenced_twice_is_listed_once_with_both_citations():
    run = uuid4()
    row = Figure("FIGURE 1.2. Surfaces.", 2, run)
    first = citation("The surfaces are described ( Fig. 1.2 ).", parse_run=run)
    second = citation("Their margins meet ( Fig. 1.2 ).", parse_run=run)
    found = resolve(Session([row]), uuid4(), [first, second])
    assert len(found) == 1
    assert found[0].citation_ids == [first.citation_id, second.citation_id]


def test_figures_are_ordered_by_page_and_capped():
    run = uuid4()
    rows = [Figure(f"FIGURE 1.{n}. Plate {n}.", 20 - n, run) for n in range(1, 10)]
    text = "( Figs. 1.1-1.9 )"
    found = resolve(Session(rows), uuid4(), [citation(text, parse_run=run)])
    assert len(found) == 6, "a passage citing a plate cannot fill the answer with thumbnails"
    assert [figure.page for figure in found] == sorted(figure.page for figure in found)


def test_no_citations_means_no_query_and_no_figures():
    session = Session([Figure("FIGURE 1.2. Surfaces.", 2, uuid4())])
    assert resolve(session, uuid4(), []) == []
    assert session.queried == 0


def test_a_figure_carries_no_field_that_could_assert_evidence():
    """It is source material beside an answer, not a claim about one."""
    allowed = {
        "figure_id",
        "document_id",
        "document_version_id",
        "parse_run_id",
        "document_title",
        "page",
        "caption",
        "label",
        "linked_by",
        "citation_ids",
    }
    assert set(AskFigure.model_fields) == allowed
