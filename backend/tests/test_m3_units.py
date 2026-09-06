import json
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.chunking_config import ChunkingConfig
from app.ingestion.chunking.builder import Builder
from app.ingestion.chunking.errors import ChunkError, retryable
from app.ingestion.chunking.model import ChunkInput, Span
from app.ingestion.chunking.quality import omitted_characters, validate
from app.ingestion.chunking.tokenizer import LocalTokenizer
from pydantic import ValidationError

GOLD = json.loads(
    (Path(__file__).parent / "fixtures/chunking/gold.json").read_text(encoding="utf-8")
)["cases"]


@pytest.fixture(scope="module")
def tokenizer():
    return LocalTokenizer(ChunkingConfig())


def case(name):
    return ChunkInput.model_validate(next(c["source"] for c in GOLD if c["id"] == name))


@pytest.mark.parametrize("entry", GOLD, ids=lambda c: c["id"])
def test_gold_structure(entry, tokenizer):
    source = ChunkInput.model_validate(entry["source"])
    config = ChunkingConfig()
    output = Builder(source, config, tokenizer).build()
    expected = entry["expected"]
    chunks = output.chunks
    for kind, minimum in expected.get("types", {}).items():
        assert sum(c.kind == kind for c in chunks) >= minimum
    text = "\n".join(c.source_text for c in chunks)
    assert all(s in text for s in expected.get("required_text", []))
    assert all(s not in text for s in expected.get("forbidden_text", []))
    if expected.get("cross_page"):
        assert any(c.page_start < c.page_end for c in chunks if c.page_start and c.page_end)
    if "excluded" in expected:
        assert len(output.excluded_element_ids) == expected["excluded"]
    if "question_count" in expected:
        assert len(output.questions) == expected["question_count"]
    if "answers" in expected:
        assert [q.explicit_answer for q in output.questions] == expected["answers"]
    if expected.get("options_atomic"):
        for q in output.questions:
            chunk = next(c for c in chunks if c.question_key == q.key and c.kind == "QUESTION")
            assert all(o.text in chunk.source_text for o in q.options)
            assert not q.answer_inferred and q.authority["authority_level"] == "ASSESSMENT"
    if expected.get("table_headers"):
        assert all(
            c.metadata["headers"] and "Unit" in c.source_text
            for c in chunks
            if c.kind == "TABLE_PART"
        )
    for a, b in expected.get("separate_elements", []):
        assert not any(
            {source.elements[a].id, source.elements[b].id} <= {s.element_id for s in c.spans}
            for c in chunks
            if c.kind == "TEXT_CHILD"
        )
    if expected.get("ordered_fragments"):
        children = "\n".join(c.source_text for c in chunks if c.kind == "TEXT_CHILD")
        positions = [children.index(s) for s in expected["ordered_fragments"]]
        assert positions == sorted(positions)
    if expected.get("separate_artifacts"):
        assert all(len(c.artifact_ids) == 1 for c in chunks if c.kind == "TABLE")
    result, findings, _ = validate(source, output, config, tokenizer)
    assert result in {"PASS", "PASS_WITH_WARNINGS"}, [(f.code, f.severity) for f in findings]


@pytest.mark.parametrize("entry", GOLD, ids=lambda c: c["id"])
def test_identical_input_is_deterministic(entry, tokenizer):
    source = ChunkInput.model_validate(entry["source"])
    config = ChunkingConfig()
    assert Builder(source, config, tokenizer).build() == Builder(source, config, tokenizer).build()


def test_borderline_labels_do_not_change_chunk_hashes(tokenizer):
    source = case("paragraphs")
    changed = source.model_copy(
        update={
            "elements": tuple(
                e.model_copy(update={"kind": "CAPTION" if i % 2 else "PAGE_HEADER"})
                for i, e in enumerate(source.elements)
            )
        }
    )
    first = Builder(source, ChunkingConfig(), tokenizer).build()
    second = Builder(changed, ChunkingConfig(), tokenizer).build()
    assert [c.key for c in first.chunks] == [c.key for c in second.chunks]


def test_tokenizer_preserves_source_offsets_and_counts_subwords(tokenizer):
    text = "ABC Δ-dose: sodium/potassium; 135–145 mmol/L."
    assert tokenizer.count(text) > len(text.split())
    source = case("paragraphs")
    e = source.elements[0].model_copy(update={"text": text * 60})
    source = source.model_copy(update={"elements": (e,)})
    output = Builder(source, ChunkingConfig(child_target_tokens=32), tokenizer).build()
    spans = [s for c in output.chunks if c.kind == "TEXT_CHILD" for s in c.spans]
    assert "".join(e.text[s.start : s.end] for s in spans) == e.text
    assert all(c.token_count <= 32 for c in output.chunks if c.kind == "TEXT_CHILD")


def test_missing_or_tampered_tokenizer_fails_closed(tmp_path):
    broken = tmp_path / "tokenizer.json"
    broken.write_text("{}")
    with pytest.raises(ChunkError, match="CHUNK_TOKENIZER_LOAD_FAILED"):
        LocalTokenizer(ChunkingConfig(), broken)


def test_policy_fingerprint_covers_thresholds_and_targets():
    config = ChunkingConfig()
    assert config.fingerprint != config.model_copy(update={"child_target_tokens": 400}).fingerprint
    assert (
        config.fingerprint
        != config.model_copy(
            update={"thresholds": config.thresholds.model_copy(update={"tiny_tokens": 30})}
        ).fingerprint
    )
    with pytest.raises(ValidationError):
        ChunkingConfig(child_target_tokens=1000, parent_target_tokens=500)
    with pytest.raises(ValidationError):
        ChunkingConfig(overlap_tokens=20)


def test_invalid_table_fails_without_synthetic_repair(tokenizer):
    source = case("table")
    a = source.artifacts[0]
    source = source.model_copy(
        update={"artifacts": (a.model_copy(update={"data": {**a.data, "row_count": 0}}),)}
    )
    with pytest.raises(ChunkError, match="CHUNK_TABLE_FAILED"):
        Builder(source, ChunkingConfig(), tokenizer).build()


def test_merged_rows_are_not_split(tokenizer):
    source = case("large-table")
    a = source.artifacts[0]
    cells = [dict(c) for c in a.data["cells"]]
    cells[3]["row_span"] = 4
    source = source.model_copy(
        update={"artifacts": (a.model_copy(update={"data": {**a.data, "cells": cells}}),)}
    )
    output = Builder(source, ChunkingConfig(table_max_tokens=32), tokenizer).build()
    part = next(c for c in output.chunks if 1 in c.metadata["row_indexes"])
    assert {1, 2, 3, 4} <= set(part.metadata["row_indexes"])


def test_validation_detects_missing_mapping_and_wrong_token_count(tokenizer):
    source = case("paragraphs")
    config = ChunkingConfig()
    output = Builder(source, config, tokenizer).build()
    changed = output.chunks[0].model_copy(
        update={"token_count": -1, "spans": (Span(element_id=uuid4(), end=4),)}
    )
    result, findings, _ = validate(
        source,
        output.model_copy(update={"chunks": (changed,) + output.chunks[1:]}),
        config,
        tokenizer,
    )
    assert result == "FAIL"
    assert {"CHUNK_SOURCE_INVALID", "CHUNK_TOKEN_COUNT_INVALID"} <= {f.code for f in findings}


@pytest.mark.parametrize(
    "code,expected",
    [
        ("CHUNK_PERSISTENCE_FAILED", True),
        ("CHUNK_TIMEOUT", True),
        ("CHUNK_TABLE_FAILED", False),
        ("CHUNK_VALIDATION_FAILED", False),
    ],
)
def test_retryability(code, expected):
    assert retryable(code) == expected


# ------------------------------------------------------------------- source coverage


def test_source_coverage_survives_aggressive_splitting(tokenizer):
    """Every gold fixture keeps all of its non-whitespace source text under a small budget."""
    config = ChunkingConfig(
        child_target_tokens=32,
        parent_target_tokens=128,
        table_max_tokens=32,
        explanation_max_tokens=32,
    )
    for entry in GOLD:
        source = ChunkInput.model_validate(entry["source"])
        _, _, metrics = validate(
            source, Builder(source, config, tokenizer).build(), config, tokenizer
        )
        assert metrics["missing_provenance"] == 0, entry["id"]
        assert metrics["omitted_characters"] == 0, entry["id"]
        assert metrics["split_omitted_characters"] == 0, entry["id"]


def test_dropped_source_text_inside_a_mapped_element_fails(tokenizer):
    """An element that is still referenced but no longer fully covered is source loss."""
    source = case("paragraphs")
    config = ChunkingConfig()
    output = Builder(source, config, tokenizer).build()
    truncated = tuple(
        chunk.model_copy(
            update={
                "spans": tuple(
                    span.model_copy(update={"end": max(span.start, span.end - 40)})
                    for span in chunk.spans
                )
            }
        )
        for chunk in output.chunks
    )
    result, findings, metrics = validate(
        source, output.model_copy(update={"chunks": truncated}), config, tokenizer
    )
    assert result == "FAIL"
    assert "CHUNK_SOURCE_TEXT_OMITTED" in {f.code for f in findings}
    assert metrics["omitted_characters"] > 0


def test_text_only_a_parent_still_carries_is_reported_as_a_split_defect(tokenizer):
    source = case("paragraphs")
    config = ChunkingConfig()
    output = Builder(source, config, tokenizer).build()
    index = next(i for i, c in enumerate(output.chunks) if c.kind == "TEXT_CHILD")
    child = output.chunks[index]
    last = child.spans[-1]
    shortened = child.model_copy(
        update={
            "spans": child.spans[:-1] + (last.model_copy(update={"end": last.end - 40}),),
        }
    )
    result, findings, metrics = validate(
        source,
        output.model_copy(
            update={"chunks": output.chunks[:index] + (shortened,) + output.chunks[index + 1 :]}
        ),
        config,
        tokenizer,
    )
    assert result == "NEEDS_REVIEW"
    assert "CHUNK_SPLIT_TEXT_OMITTED" in {f.code for f in findings}
    assert metrics["omitted_characters"] == 0 and metrics["split_omitted_characters"] > 0


def test_whitespace_between_spans_is_not_reported_as_loss(tokenizer):
    element = case("paragraphs").elements[0].model_copy(update={"text": "Alpha    beta."})
    assert omitted_characters(element, [(0, 5), (9, 14)]) == 0
    assert omitted_characters(element, [(0, 5)]) == len("beta.")


# ------------------------------------------------------------------- phases and metrics


def test_phases_are_announced_only_where_work_happens(tokenizer):
    seen: list[str] = []
    source = case("table")
    Builder(source, ChunkingConfig(), tokenizer, phase=seen.append).build()
    assert seen == ["CHUNK_TABLES_STARTED", "CHUNK_TEXT_STARTED"]

    questions: list[str] = []
    Builder(case("mcq"), ChunkingConfig(), tokenizer, phase=questions.append).build()
    assert "CHUNK_QUESTIONS_STARTED" in questions
    assert "CHUNK_TABLES_STARTED" not in questions
    assert "CHUNK_FIGURES_STARTED" not in questions


def test_metrics_report_structure_without_claiming_accuracy(tokenizer):
    source = case("large-table")
    config = ChunkingConfig()
    output = Builder(source, config, tokenizer).build()
    _, _, metrics = validate(source, output, config, tokenizer)
    for key in (
        "chunks",
        "parents",
        "children",
        "eligible_elements",
        "mapped_elements",
        "missing_provenance",
        "question_option_splits",
        "table_header_loss",
        "table_parts",
        "formulas",
        "figures",
        "questions",
        "child_tokens",
        "parent_tokens",
        "tiny",
        "oversized",
    ):
        assert key in metrics, key
    assert metrics["table_header_loss"] == 0
    assert metrics["mapped_elements"] == metrics["eligible_elements"]
    # Nothing in the metric vocabulary claims retrieval or medical accuracy.
    assert not [k for k in metrics if "accuracy" in k or "recall" in k or "precision" in k]


def test_repeated_table_headers_are_identical_across_parts(tokenizer):
    source = case("large-table")
    output = Builder(source, ChunkingConfig(table_max_tokens=48), tokenizer).build()
    parts = [c for c in output.chunks if c.kind == "TABLE_PART"]
    assert len(parts) > 1
    assert len({c.metadata["headers"] for c in parts}) == 1
    assert all(c.metadata["headers"] in c.source_text for c in parts)
    rows = [i for c in parts for i in c.metadata["row_indexes"]]
    assert rows == sorted(rows) and len(rows) == len(set(rows))
