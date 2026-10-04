"""Every retrieval unit fits the embedding budget by construction, measured as it is embedded.

The real case behind these tests: an anatomy table whose innervation cell spanned rows 2–9. The
splitter treated the eight rows as one indivisible group, and the group alone measured 396
tokens against a 384-token budget, so validation stopped the document with CHUNK_OVERSIZED.
Validation was right. The fix is in construction: the budget is unchanged, nothing is truncated,
and a merged cell that has to be divided is carried, by name, into each part it describes.

Everything is measured with the production MedCPT tokenizer (`LocalTokenizer`, which verifies its
pinned artifact), on `retrieval_text` — the string the encoder receives.
"""

import json
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from app.core.chunking_config import ChunkingConfig
from app.ingestion.chunking.builder import Builder
from app.ingestion.chunking.model import (
    ChunkInput,
    DraftChunk,
    SourceArtifact,
    SourceElement,
    SourcePage,
)
from app.ingestion.chunking.quality import validate
from app.ingestion.chunking.tokenizer import LocalTokenizer

GOLD = json.loads(
    (Path(__file__).parent / "fixtures/chunking/gold.json").read_text(encoding="utf-8")
)["cases"]
EMBEDDED = {
    "TEXT_CHILD",
    "LIST",
    "TABLE",
    "TABLE_PART",
    "FORMULA",
    "FIGURE_CONTEXT",
    "QUESTION",
    "QUESTION_EXPLANATION",
    "OTHER_STRUCTURED",
}


def uid(name: str):
    return uuid5(NAMESPACE_URL, f"chunk-budget/{name}")


@pytest.fixture(scope="module")
def tokenizer():
    return LocalTokenizer(ChunkingConfig())


PAGE = SourcePage(id=uid("page"), number=10, width=600, height=800)
HEADER = ["Intrinsic muscles", "Origin", "Insertion", "Innervation", "Function"]
MUSCLES = [
    (
        "Cricothyroid",
        "Anterolateral cricoid cartilage",
        "Inferior thyroid cartilage",
        "External laryngeal nerve",
        "Tenses vocal folds",
    ),
    (
        "Posterior cricoarytenoid",
        "Posterior surface of cricoid lamina",
        "Muscular process of arytenoid cartilage",
        None,
        "Abducts and lengthens vocal folds",
    ),
    (
        "Lateral cricoarytenoid",
        "Arch of cricoid cartilage",
        "Muscular process of arytenoid",
        None,
        "Adducts and shortens vocal folds",
    ),
    (
        "Transverse arytenoid",
        "Lateral border of arytenoid",
        "Opposite arytenoid cartilage",
        None,
        "Adducts arytenoid cartilages",
    ),
    (
        "Oblique arytenoid",
        "Muscular process of arytenoid",
        "Apex of contralateral arytenoid",
        None,
        "Acts as a sphincter on the laryngeal inlet",
    ),
    (
        "Aryepiglottic muscle",
        "Apex of arytenoid cartilage",
        "Lateral border of epiglottis",
        None,
        "Closes the laryngeal inlet",
    ),
    (
        "Thyroarytenoid",
        "Angle of thyroid cartilage",
        "Anterolateral arytenoid surface",
        None,
        "Relaxes the vocal ligament",
    ),
    (
        "Vocalis",
        "Vocal process of arytenoid cartilage",
        "Ipsilateral vocal ligament",
        None,
        "Maintains tension of the anterior vocal ligament",
    ),
    (
        "Thyroepiglottic muscle",
        "Angle of thyroid cartilage",
        "Lateral aspect of epiglottis",
        None,
        "Widens the laryngeal inlet",
    ),
]
# The parser wrote the merged cell's value once per covered row, as Docling did on the real page.
NERVE = " ".join(["Inferior laryngeal nerve (of recurrent laryngeal nerve (CN X))"] * 8)


def muscle_table(caption: str | None = None, extra_cells=None, hierarchy: bool = False):
    """Header row, nine muscles, and one innervation cell spanning rows 2–9."""
    cells = [
        {"row": 0, "column": c, "text": t, "column_header": True} for c, t in enumerate(HEADER)
    ]
    for r, values in enumerate(MUSCLES, start=1):
        for c, value in enumerate(values):
            if value is not None:
                cells.append({"row": r, "column": c, "text": value})
    cells.append({"row": 2, "column": 3, "row_span": 8, "text": NERVE})
    cells.extend(extra_cells or [])
    heading = SourceElement(
        id=uid("heading"),
        page_id=PAGE.id,
        page_number=10,
        reading_order=0,
        kind="HEADING",
        text="Larynx > Intrinsic muscles of the larynx",
    )
    table = SourceElement(
        id=uid("table"),
        page_id=PAGE.id,
        page_number=10,
        reading_order=1,
        kind="TABLE",
        parent_id=heading.id if hierarchy else None,
    )
    return ChunkInput(
        document_id=uid("doc"),
        version_id=uid("version"),
        parse_run_id=uid("parse"),
        title="Synthetic muscle chart",
        source_type="TEXTBOOK",
        authority_level="REFERENCE",
        pages=(PAGE,),
        elements=(heading, table) if hierarchy else (table,),
        artifacts=(
            SourceArtifact(
                id=uid("artifact"),
                element_id=table.id,
                kind="TABLE",
                page_number=10,
                caption=caption,
                data={
                    "row_count": len(MUSCLES) + 1,
                    "column_count": len(HEADER),
                    "header_row_count": 1,
                    "cells": cells,
                },
            ),
        ),
    )


def build(source, tokenizer, **policy):
    config = ChunkingConfig(**policy)
    return Builder(source, config, tokenizer).build(), config


def table_chunks(dataset):
    return [c for c in dataset.chunks if c.kind in {"TABLE", "TABLE_PART"}]


def words(text: str) -> list[str]:
    return re.findall(r"\S+", text)


# ------------------------------------------------------------------------------------- 1, 2


@pytest.mark.parametrize("budget", [96, 128, 160, 200, 256, 384])
def test_no_table_part_exceeds_the_budget_as_embedded(tokenizer, budget):
    dataset, config = build(muscle_table(hierarchy=True), tokenizer, table_max_tokens=budget)
    for chunk in table_chunks(dataset):
        # Measured with the production tokenizer, on the string the encoder receives.
        assert chunk.retrieval_token_count == tokenizer.count(chunk.retrieval_text)
        assert chunk.retrieval_token_count <= budget, chunk.metadata["row_indexes"]


def test_the_real_shape_no_longer_needs_review(tokenizer):
    # The real page: rows 2-9 shared one merged cell and together exceeded the budget, so they
    # were emitted as one over-budget part. The synthetic table is smaller than the real one, so
    # the same relationship is reproduced at a proportionally smaller budget.
    source = muscle_table()
    budget = 256
    whole = Builder(source, ChunkingConfig(table_max_tokens=2048), tokenizer).build()
    merged_rows = table_chunks(whole)[0].retrieval_token_count - tokenizer.count(
        " | ".join(MUSCLES[0])
    )
    assert merged_rows > budget
    dataset, config = build(source, tokenizer, table_max_tokens=budget)
    parts = table_chunks(dataset)
    assert not any(set(range(2, 10)) <= set(p.metadata["row_indexes"]) for p in parts)
    assert all(p.retrieval_token_count <= budget for p in parts)
    result, findings, _ = validate(source, dataset, config, tokenizer)
    assert result in {"PASS", "PASS_WITH_WARNINGS"}, [(f.code, f.severity) for f in findings]
    assert not [f for f in findings if f.code == "CHUNK_OVERSIZED"]


def test_the_budget_itself_is_unchanged():
    config = ChunkingConfig()
    assert config.table_max_tokens == 384 and config.retrieval_budget_tokens == 384
    assert config.chunker_version == "1.1.0"


def test_a_new_chunker_version_is_never_mistaken_for_an_old_run():
    # Reuse requires an identical policy fingerprint; runs built by 1.0.0 must not be reused.
    current = ChunkingConfig().model_dump()
    assert ChunkingConfig().fingerprint != _fingerprint({**current, "chunker_version": "1.0.0"})


def _fingerprint(values):
    import hashlib

    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------------- 3, 4, 5


def test_every_part_keeps_the_header_row_and_its_provenance(tokenizer):
    dataset, _ = build(
        muscle_table(caption="Table 3. Intrinsic muscles"), tokenizer, table_max_tokens=160
    )
    parts = table_chunks(dataset)
    assert len(parts) > 1
    header = " | ".join(HEADER)
    for part in parts:
        assert header in part.source_text
        assert part.source_text.startswith("Table 3. Intrinsic muscles")
        assert part.metadata["headers"] == header
        assert part.artifact_ids == (uid("artifact"),)
        assert uid("table") in {s.element_id for s in part.spans}
        assert part.page_start == part.page_end == 10
    rows = [r for p in parts for r in p.metadata["row_indexes"]]
    assert rows == sorted(set(rows)) == list(range(1, len(MUSCLES) + 1))


def test_a_divided_merged_cell_is_carried_by_name_into_every_part_it_describes(tokenizer):
    dataset, _ = build(muscle_table(), tokenizer, table_max_tokens=200)
    parts = table_chunks(dataset)
    covered = set(range(2, 10))
    for part in parts:
        rows = set(part.metadata["row_indexes"])
        if rows & covered and 2 not in rows:
            assert (
                "Innervation (merged cell, also applies to these rows): " + NERVE
                in part.source_text
            )
            assert [c["row"] for c in part.metadata["carried_cells"]] == [2]
        else:
            assert "carried_cells" not in part.metadata
    # The value is carried, never written into each row as though it were separate cells.
    assert all(part.source_text.count(NERVE) <= 1 for part in parts)


def test_no_row_or_cell_is_lost(tokenizer):
    source = muscle_table()
    dataset, _ = build(source, tokenizer, table_max_tokens=128)
    text = "\n".join(c.source_text for c in table_chunks(dataset))
    for cell in source.artifacts[0].data["cells"]:
        assert cell["text"] in text, cell


# ------------------------------------------------------------------------------------- 6, 7


def test_a_table_exactly_at_the_budget_is_one_chunk_and_one_over_is_split(tokenizer):
    source = muscle_table()
    whole = table_chunks(Builder(source, ChunkingConfig(table_max_tokens=2048), tokenizer).build())[
        0
    ]
    exact = whole.retrieval_token_count
    at, _ = build(source, tokenizer, table_max_tokens=exact)
    assert [c.kind for c in table_chunks(at)] == ["TABLE"]
    assert table_chunks(at)[0].retrieval_token_count == exact
    under, _ = build(source, tokenizer, table_max_tokens=exact - 1)
    assert len(table_chunks(under)) > 1
    assert all(c.retrieval_token_count <= exact - 1 for c in table_chunks(under))


# ----------------------------------------------------------------------------------------- 8


def test_a_single_row_too_large_for_any_part_is_divided_by_cells_deterministically(tokenizer):
    long_function = " ".join(
        f"Clause {i} describes how the muscle acts during phonation and swallowing."
        for i in range(40)
    )
    cells = [{"row": 1, "column": 4, "text": long_function}]
    source = muscle_table()
    base = [
        c for c in source.artifacts[0].data["cells"] if not (c["row"] == 1 and c["column"] == 4)
    ]
    a = source.artifacts[0]
    source = source.model_copy(
        update={"artifacts": (a.model_copy(update={"data": {**a.data, "cells": base + cells}}),)}
    )
    first, _ = build(source, tokenizer, table_max_tokens=128)
    again, _ = build(source, tokenizer, table_max_tokens=128)
    assert first == again
    # At this budget the carried merged cell alone nearly fills a part, so rows 2-9 are divided by
    # cells as well; row 1 is the one with the oversized cell of its own.
    assert all(c.retrieval_token_count <= 128 for c in table_chunks(first))
    fragments = [
        c
        for c in table_chunks(first)
        if c.metadata.get("row_fragment") and c.metadata["row_indexes"] == [1]
    ]
    assert len(fragments) > 1
    # Each fragment names its row and keeps the header context.
    assert all("Intrinsic muscles: Cricothyroid" in c.source_text for c in fragments)
    assert all(" | ".join(HEADER) in c.source_text for c in fragments)
    # Every word of the long cell survives, in order, across the fragments.
    joined = " ".join(
        line.split(": ", 1)[1]
        for c in fragments
        for line in c.source_text.splitlines()
        if line.startswith("Function")
    )
    assert words(joined) == words(long_function)
    for value in (
        "Anterolateral cricoid cartilage",
        "Inferior thyroid cartilage",
        "External laryngeal nerve",
    ):
        assert sum(value in c.source_text for c in fragments) == 1


# ---------------------------------------------------------------------------- 9, 10: elsewhere


def case(name):
    return ChunkInput.model_validate(next(c["source"] for c in GOLD if c["id"] == name))


@pytest.mark.parametrize("entry", GOLD, ids=lambda c: c["id"])
def test_every_embedded_unit_in_the_gold_corpus_fits_as_embedded(entry, tokenizer):
    source = ChunkInput.model_validate(entry["source"])
    dataset, config = build(source, tokenizer)
    for chunk in dataset.chunks:
        assert chunk.retrieval_token_count == tokenizer.count(chunk.retrieval_text)
        if chunk.kind in EMBEDDED:
            assert chunk.retrieval_token_count <= config.retrieval_budget_tokens, chunk.kind


def test_text_children_budget_for_their_hierarchy_prefix(tokenizer):
    # A long paragraph under a deep heading path: the split must leave room for the prefix the
    # child is embedded with, not only for its own source text.
    source = case("paragraphs")
    heading = SourceElement(
        id=uid("deep-heading"),
        page_id=source.elements[0].page_id,
        page_number=source.elements[0].page_number,
        reading_order=-1,
        kind="HEADING",
        text=" ".join(["Respiratory system, larynx and the intrinsic laryngeal muscles"] * 3),
    )
    long = " ".join(
        f"Sentence {i} explains a laryngeal relation in plain detail." for i in range(120)
    )
    body = SourceElement(
        id=uid("long-paragraph"),
        page_id=heading.page_id,
        page_number=heading.page_number,
        reading_order=0,
        parent_id=heading.id,
        text=long,
    )
    source = source.model_copy(update={"elements": (heading, body), "artifacts": ()})
    dataset, config = build(source, tokenizer, child_target_tokens=96)
    children = [
        c
        for c in dataset.chunks
        if c.kind == "TEXT_CHILD" and {s.element_id for s in c.spans} == {uid("long-paragraph")}
    ]
    assert len(children) > 1 and all(c.retrieval_text.startswith("Context: ") for c in children)
    assert all(c.retrieval_token_count <= 96 for c in children)
    result, findings, _ = validate(source, dataset, config, tokenizer)
    assert not [f for f in findings if f.code == "CHUNK_OVERSIZED"]


def test_text_without_hierarchy_is_split_exactly_as_before(tokenizer):
    # With no prefix to pay for, the room is the target itself, so boundaries are the 1.0.0 ones.
    source = case("paragraphs")
    with_context, _ = build(source, tokenizer)
    without, _ = build(source, tokenizer, include_hierarchy_context=False)
    spans = [c.spans for c in with_context.chunks if c.kind == "TEXT_CHILD"]
    if all(not c.hierarchy for c in with_context.chunks):
        assert spans == [c.spans for c in without.chunks if c.kind == "TEXT_CHILD"]


# ---------------------------------------------------------------------------------------- 11


def test_the_validator_still_refuses_an_oversized_unit_that_reaches_it(tokenizer):
    source = muscle_table()
    dataset, config = build(source, tokenizer)
    victim = table_chunks(dataset)[0]
    padded = victim.retrieval_text + "\n" + " ".join(["padding"] * 400)
    forged: DraftChunk = victim.model_copy(
        update={
            "retrieval_text": padded,
            "retrieval_token_count": tokenizer.count(padded),
        }
    )
    forged_set = dataset.model_copy(
        update={"chunks": tuple(forged if c.key == victim.key else c for c in dataset.chunks)}
    )
    result, findings, _ = validate(source, forged_set, config, tokenizer)
    assert result == "NEEDS_REVIEW"
    assert ("CHUNK_OVERSIZED", "ERROR") in {(f.code, f.severity) for f in findings}
