"""Generate synthetic normalized parse datasets with fixed identities and gold expectations."""

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.ingestion.chunking.model import ChunkInput, SourceArtifact, SourceElement, SourcePage

ROOT = Path("backend/tests/fixtures/chunking")
ROOT.mkdir(parents=True, exist_ok=True)


def uid(name):
    return uuid5(NAMESPACE_URL, "medrag-m3-fixture/" + name)


def dataset(
    name,
    texts,
    *,
    pages=None,
    kinds=None,
    parents=None,
    artifacts=(),
    source_type="TEXTBOOK",
    boxes=None,
):
    pages = pages or [1] * len(texts)
    sheet = {
        n: SourcePage(id=uid(name + f"/page/{n}"), number=n, width=600, height=800)
        for n in sorted(set(pages))
    }
    elements = tuple(
        SourceElement(
            id=uid(name + f"/element/{i}"),
            page_id=sheet[n].id,
            page_number=n,
            reading_order=i,
            text=t,
            kind=(kinds or {}).get(i, "PARAGRAPH"),
            parent_id=uid(name + f"/element/{parents[i]}") if parents and i in parents else None,
            bbox=(boxes or {}).get(i, (40, 100 + i % 5 * 70, 560, 140 + i % 5 * 70)),
        )
        for i, (t, n) in enumerate(zip(texts, pages, strict=True))
    )
    return ChunkInput(
        document_id=uid(name + "/doc"),
        version_id=uid(name + "/version"),
        parse_run_id=uid(name + "/parse"),
        title="Synthetic " + name,
        source_type=source_type,
        authority_level="ASSESSMENT" if source_type == "QUESTION_BANK" else "UNREVIEWED",
        edition="Synthetic first",
        publication_year=2026,
        pages=tuple(sheet.values()),
        elements=elements,
        artifacts=tuple(artifacts),
    )


def artifact(source, index, kind, **data):
    return SourceArtifact(
        id=uid(str(source.parse_run_id) + f"/{kind}/{index}"),
        element_id=source.elements[index].id,
        kind=kind,
        page_number=source.elements[index].page_number,
        data=data,
    )


def with_artifacts(source, artifacts):
    return source.model_copy(update={"artifacts": tuple(artifacts)})


def table(rows):
    cells = [
        {"row": 0, "column": c, "text": v, "row_span": 1, "col_span": 1, "column_header": True}
        for c, v in enumerate(["Marker", "Reading", "Unit"])
    ]
    for r in range(1, rows):
        for c, value in enumerate([f"Synthetic marker {r}", f"{130 + r}.5", "example units"]):
            cells.append({"row": r, "column": c, "text": value, "row_span": 1, "col_span": 1})
    return {"row_count": rows, "column_count": 3, "header_row_count": 1, "cells": cells}


cases = []


def add(name, source, **expected):
    cases.append({"id": name, "source": source.model_dump(mode="json"), "expected": expected})


paragraph = (
    "Source fidelity preserves exact terminology and units. "
    "This sentence supplies synthetic context for a parsing fixture. "
)
s = dataset(
    "sections",
    [
        "Section one",
        paragraph * 12,
        "Section two",
        paragraph.replace("terminology", "symbols") * 12,
    ],
    kinds={0: "HEADING", 2: "HEADING"},
    parents={1: 0, 3: 2},
)
add("sections", s, types={"TEXT_PARENT": 2, "TEXT_CHILD": 2}, separate_elements=[[1, 3]])
s = dataset("paragraphs", [paragraph * 5] * 6)
add("paragraphs", s, types={"TEXT_PARENT": 1, "TEXT_CHILD": 2}, all_source_mapped=True)
s = dataset(
    "table",
    ["Synthetic table", "Measured markers", "Qualifiers remain in source."],
    kinds={0: "TABLE", 1: "CAPTION"},
)
a = artifact(s, 0, "TABLE", **table(4)).model_copy(
    update={"caption_id": s.elements[1].id, "caption": "Measured markers"}
)
add(
    "table",
    with_artifacts(s, [a]),
    types={"TABLE": 1},
    required_text=["Marker", "Reading", "Unit", "Synthetic marker 3"],
)
s = dataset("large-table", ["Large canonical table"], kinds={0: "TABLE"})
add(
    "large-table",
    with_artifacts(s, [artifact(s, 0, "TABLE", **table(90))]),
    types={"TABLE_PART": 2},
    table_headers=True,
)
s = dataset(
    "formula",
    ["M = (A + 2 × B) / 3", "M is the source-defined measure; A and B are source labels."],
    kinds={0: "FORMULA"},
)
a = artifact(s, 0, "FORMULA", source_expression=s.elements[0].text).model_copy(
    update={"related_ids": (s.elements[1].id,)}
)
add(
    "formula",
    with_artifacts(s, [a]),
    types={"FORMULA": 1},
    required_text=["M = (A + 2 × B) / 3", "source-defined measure"],
)
s = dataset(
    "figure",
    [
        "",
        "Figure 1. Two original circles connected by a line.",
        "The source explains the diagram without generated interpretation.",
    ],
    kinds={0: "FIGURE", 1: "CAPTION"},
)
a = artifact(s, 0, "FIGURE", image_available=True).model_copy(
    update={"caption_id": s.elements[1].id, "caption": s.elements[1].text}
)
add(
    "figure",
    with_artifacts(s, [a]),
    types={"FIGURE_CONTEXT": 1},
    required_text=["Two original circles"],
)
s = dataset(
    "columns",
    [
        "Left column first. " + paragraph * 8,
        "Left column second. " + paragraph * 8,
        "Right column first. " + paragraph * 8,
        "Right column second. " + paragraph * 8,
    ],
    boxes={
        0: (40, 100, 280, 250),
        1: (40, 300, 280, 450),
        2: (320, 100, 560, 250),
        3: (320, 300, 560, 450),
    },
)
add(
    "columns",
    s,
    types={"TEXT_CHILD": 2},
    ordered_fragments=[
        "Left column first.",
        "Left column second.",
        "Right column first.",
        "Right column second.",
    ],
)
s = dataset("page-transition", [paragraph * 3, paragraph * 3], pages=[1, 2])
add("page-transition", s, cross_page=True, all_source_mapped=True)
s = dataset(
    "mcq",
    [
        "1. Which label appears in this synthetic source?",
        "A. Alpha",
        "B. Beta",
        "C. Gamma",
        "D. Delta",
        "Answer: B",
        "2. Which label is not answered in the source?",
        "A. North",
        "B. South",
    ],
    source_type="QUESTION_BANK",
)
add("mcq", s, types={"QUESTION": 2}, question_count=2, answers=["B", None], options_atomic=True)
s = dataset(
    "explanation",
    [
        "1. Which label is explicit?",
        "A. First",
        "B. Second",
        "Answer: A",
        "Explanation: " + paragraph * 70,
    ],
    source_type="QUESTION_BANK",
)
add(
    "explanation",
    s,
    types={"QUESTION": 1, "QUESTION_EXPLANATION": 2},
    question_count=1,
    answers=["A"],
    options_atomic=True,
)
s = dataset(
    "margins",
    [
        "Synthetic reference 1",
        paragraph * 3,
        "Synthetic reference 2",
        paragraph * 3,
        "Synthetic reference 3",
        paragraph * 3,
    ],
    pages=[1, 1, 2, 2, 3, 3],
    kinds={0: "PAGE_HEADER", 2: "PARAGRAPH", 4: "CAPTION"},
    boxes={0: (30, 10, 500, 35), 2: (30, 10, 500, 35), 4: (30, 10, 500, 35)},
)
add("margins", s, excluded=3, forbidden_text=["Synthetic reference"])
s = dataset(
    "list",
    ["Source qualifications:"] + ["- Item " + str(i) + ": " + paragraph * 3 for i in range(8)],
)
add("list", s, types={"LIST": 2}, required_text=["Source qualifications:"], all_source_mapped=True)
s = dataset(
    "footnote",
    [
        "Source statement requiring a qualifier. " + paragraph * 3,
        "* This source qualification must remain accessible.",
    ],
    kinds={1: "FOOTNOTE"},
)
add("footnote", s, required_text=["source qualification"], all_source_mapped=True)
s = dataset(
    "continued-table",
    ["Table part one", "Table part two"],
    pages=[1, 2],
    kinds={0: "TABLE", 1: "TABLE"},
)
a = artifact(s, 0, "TABLE", **table(4))
b = artifact(s, 1, "TABLE", **table(4), possible_continuation=True, continuation_of_id=str(a.id))
add("continued-table", with_artifacts(s, [a, b]), types={"TABLE": 2}, separate_artifacts=True)
(ROOT / "gold.json").write_text(
    json.dumps(
        {
            "schema": "chunk-gold-m3-v1",
            "kind": "synthetic-structure-not-medical-gold",
            "cases": cases,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
print(f"Wrote {len(cases)} original normalized-input gold cases.")
