"""A FIGURE_CONTEXT chunk must be embeddable by construction, like every other retrieval unit.

A real 22-page atlas chapter ("Cerebellum and Fourth Ventricle", 18 figures, 0 tables) produced one
FIGURE_CONTEXT of 641 tokens against a 384-token retrieval budget. The figure branch of the builder
emitted the figure element, its caption and its linked neighbours as a single chunk with no budget
of any kind: tables were split at `table_max_tokens` and prose packed at `child_target_tokens`, but
a figure legend was whatever the document happened to contain. Anatomy atlases contain long ones.

Two things are protected here. A figure's legend is *split* rather than trimmed — the caption
element is consumed by the figure chunk, so text left outside a bounded representation would sit in
no retrieval unit at all — and the budget is measured on the retrieval representation, which is the
string the encoder actually receives as its input body.

CHUNK_FIGURE_NO_TEXT is deliberately untouched: a figure with no caption and no textual neighbour is
still exactly one chunk carrying exactly that warning.
"""

import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from app.core.chunking_config import ChunkingConfig
from app.core.embedding_config import EmbeddingConfig
from app.embeddings.inputs import ChunkSource, build, eligible
from app.ingestion.chunking.builder import Builder
from app.ingestion.chunking.model import ChunkInput
from app.ingestion.chunking.quality import validate
from app.ingestion.chunking.tokenizer import LocalTokenizer
from app.sufficiency.question import FIGURE_CHUNKS as SUFFICIENCY_FIGURE_CHUNKS
from app.verification.claims import FIGURE_CHUNKS as CLAIM_FIGURE_CHUNKS

GOLD = json.loads(
    (Path(__file__).parent / "fixtures/chunking/gold.json").read_text(encoding="utf-8")
)["cases"]
CACHE = Path(".local/models/embeddings")
MODEL = pytest.mark.skipif(
    not CACHE.exists(), reason="Run scripts/provision_embedding_model.py first"
)

#: A figure legend in the shape the real document uses: one label sentence followed by many
#: descriptive ones, all of it source text belonging to the figure rather than to the body prose.
LEGEND = "FIGURE 1.7. Brainstem, petrosal surface, and cerebellopontine fissure. " + " ".join(
    f"The {name} is exposed along the margin of the fissure and is followed to its origin "
    f"at the surface of the {origin}, where the relationship is preserved for inspection."
    for name, origin in [
        ("superior limb", "pons"),
        ("inferior limb", "medulla"),
        ("petrosal fissure", "middle cerebellar peduncle"),
        ("flocculus", "lateral recess"),
        ("choroid plexus", "foramen of Luschka"),
        ("basilar sulcus", "anterior pontine surface"),
        ("abducens nerve", "pontomedullary sulcus"),
        ("facial nerve", "lateral end of the sulcus"),
        ("vestibulocochlear nerve", "cerebellopontine angle"),
        ("glossopharyngeal nerve", "posterior olivary margin"),
        ("vagus nerve", "postolivary sulcus"),
        ("accessory nerve", "upper cervical cord"),
    ]
)

NEIGHBOUR = (
    "The cerebellopontine fissure is formed where the cerebellum wraps around the pons, and its "
    "limbs meet laterally at an apex that the operative approach follows to the porus acusticus."
)


def figure_source(
    caption: str, *, neighbour: str | None = None, headings: tuple[str, ...] = ()
) -> ChunkInput:
    """The gold figure case with a caption of our choosing, optionally linked to a neighbour.

    `headings` become declared ancestors of the figure, which is what puts a "Context: ..." prefix
    in front of the retrieval representation — the part of the embedded body that no source text
    contains.
    """
    source = deepcopy(next(c["source"] for c in GOLD if c["id"] == "figure"))
    artifact = source["artifacts"][0]
    figure = next(e for e in source["elements"] if e["id"] == artifact["element_id"])
    caption_element = next(e for e in source["elements"] if e["id"] == artifact["caption_id"])
    caption_element["text"] = caption
    artifact["caption"] = caption
    if neighbour is not None:
        note = deepcopy(caption_element)
        note["id"] = str(uuid5(UUID(int=7), "figure-neighbour"))
        note["kind"] = "PARAGRAPH"
        note["text"] = neighbour
        note["reading_order"] = 9
        source["elements"].append(note)
        artifact["related_ids"] = [note["id"]]
    parent = None
    for index, heading in enumerate(headings):
        element = deepcopy(caption_element)
        element["id"] = str(uuid5(UUID(int=8), f"heading-{index}"))
        element["kind"] = "SECTION_HEADING"
        element["text"] = heading
        element["reading_order"] = index
        element["parent_id"] = parent
        source["elements"].append(element)
        parent = element["id"]
    if parent:
        figure["parent_id"] = parent
        caption_element["parent_id"] = parent
    return ChunkInput.model_validate(source)


def figures(output):
    return [c for c in output.chunks if c.kind == "FIGURE_CONTEXT"]


@pytest.fixture(scope="module")
def tokenizer():
    return LocalTokenizer(ChunkingConfig())


@pytest.fixture(scope="module")
def config():
    return ChunkingConfig()


# ------------------------------------------------- 1. the defect: an over-budget legend


def test_a_legend_over_the_retrieval_budget_is_split_not_emitted_whole(tokenizer, config):
    """The blocker itself. The single 641-token chunk must become several legal ones."""
    source = figure_source(LEGEND, neighbour=NEIGHBOUR)
    whole = tokenizer.count(LEGEND + "\n" + NEIGHBOUR)
    assert whole > config.retrieval_budget_tokens, (
        f"the case is only meaningful if the legend exceeds the budget ({whole} tokens)"
    )

    parts = figures(Builder(source, config, tokenizer).build())
    assert len(parts) > 1, "an over-budget legend must be split"
    for part in parts:
        assert part.retrieval_token_count <= config.retrieval_budget_tokens, (
            f"part {part.metadata.get('part_number')} of "
            f"{part.retrieval_token_count} tokens exceeds {config.retrieval_budget_tokens}"
        )


def test_the_document_that_failed_raises_no_oversized_error(tokenizer, config):
    source = figure_source(LEGEND, neighbour=NEIGHBOUR)
    output = Builder(source, config, tokenizer).build()
    _, findings, _ = validate(source, output, config, tokenizer)
    assert not [f for f in findings if f.code == "CHUNK_OVERSIZED"], (
        "a constructed dataset must not report its own chunks as oversized"
    )


def test_the_hierarchy_prefix_is_inside_the_budget(tokenizer, config):
    """The prefix is part of the embedded body and part of no source text, so it must be paid for.

    Budgeting the caption alone would emit a body of caption + "Context: ..." over the ceiling.
    """
    headings = (
        "Section 1 The Cerebellum and Fourth Ventricle",
        "Chapter 1 Microsurgical Anatomy of the Posterior Fossa",
        "Petrosal Surface and Cerebellopontine Fissure",
    )
    source = figure_source(LEGEND, headings=headings)
    parts = figures(Builder(source, config, tokenizer).build())
    assert any("Context: " in p.retrieval_text for p in parts), "the fixture must add a prefix"
    for part in parts:
        assert tokenizer.count(part.retrieval_text) <= config.retrieval_budget_tokens
        assert part.retrieval_token_count > part.token_count, (
            "the prefix must genuinely cost tokens the source text does not carry"
        )


def test_the_split_is_deterministic(tokenizer, config):
    source = figure_source(LEGEND, neighbour=NEIGHBOUR)
    first = [c.key for c in figures(Builder(source, config, tokenizer).build())]
    second = [c.key for c in figures(Builder(source, config, tokenizer).build())]
    assert first == second and len(first) > 1


# --------------------------------- 2. what is emitted fits the configured encoder contract


def test_every_emitted_retrieval_body_fits_the_encoder_contract(tokenizer, config):
    """The contract as configuration states it: body + reserved context <= encoder limit."""
    embedding = EmbeddingConfig()
    allowed = embedding.max_input_tokens - embedding.context_token_reserve
    assert config.retrieval_budget_tokens <= allowed

    source = figure_source(LEGEND, neighbour=NEIGHBOUR)
    output = Builder(source, config, tokenizer).build()
    for part in figures(output):
        assert eligible(part.kind, embedding), "figure chunks are retrieval-eligible"
        assert tokenizer.count(part.retrieval_text) <= allowed


def test_a_legend_far_over_the_budget_still_produces_only_legal_parts(tokenizer, config):
    """Ten legends end to end: the number of parts grows, the ceiling does not move."""
    source = figure_source(" ".join([LEGEND] * 10))
    parts = figures(Builder(source, config, tokenizer).build())
    assert len(parts) >= 10
    assert all(p.retrieval_token_count <= config.retrieval_budget_tokens for p in parts)


def test_a_single_sentence_longer_than_the_budget_is_still_split(tokenizer, config):
    """No sentence boundary to use: the fallback must still respect the ceiling."""
    unbroken = "the cerebellopontine fissure " * 300
    source = figure_source(unbroken)
    parts = figures(Builder(source, config, tokenizer).build())
    assert len(parts) > 1
    assert all(p.retrieval_token_count <= config.retrieval_budget_tokens for p in parts)


# ----------------------------------------- 3. the exact source stays recoverable


def test_the_complete_legend_is_recoverable_from_the_emitted_parts(tokenizer, config):
    source = figure_source(LEGEND, neighbour=NEIGHBOUR)
    elements = {e.id: e for e in source.elements}
    artifact = source.artifacts[0]
    parts = figures(Builder(source, config, tokenizer).build())

    caption_spans = [s for p in parts for s in p.spans if s.role == "CAPTION"]
    rebuilt = "".join(elements[s.element_id].text[s.start : s.end] for s in caption_spans)
    assert rebuilt == LEGEND, "the caption must be reconstructible from the parts' spans alone"

    for part in parts:
        assert part.artifact_ids == (artifact.id,), "every part stays bound to its figure"
        assert part.metadata["caption"] == LEGEND, "every part carries the complete legend"
        assert artifact.element_id in {s.element_id for s in part.spans}, (
            "every part stays mapped to the figure element"
        )
        for span in part.spans:
            assert 0 <= span.start <= span.end <= len(elements[span.element_id].text)


def test_no_source_coverage_or_omission_finding_is_raised_by_the_split(tokenizer, config):
    source = figure_source(LEGEND, neighbour=NEIGHBOUR)
    output = Builder(source, config, tokenizer).build()
    _, findings, metrics = validate(source, output, config, tokenizer)
    codes = {f.code for f in findings}
    assert not codes & {
        "CHUNK_SOURCE_COVERAGE",
        "CHUNK_SOURCE_TEXT_OMITTED",
        "CHUNK_SPLIT_TEXT_OMITTED",
        "CHUNK_SOURCE_MISSING",
        "CHUNK_SOURCE_INVALID",
        "CHUNK_ARTIFACT_MISSING",
        "CHUNK_PAGE_RANGE_INVALID",
    }
    assert metrics["omitted_characters"] == 0 and metrics["missing_provenance"] == 0


def test_part_numbering_states_how_many_parts_there_are(tokenizer, config):
    parts = figures(Builder(figure_source(LEGEND), config, tokenizer).build())
    numbers = [p.metadata["part_number"] for p in parts]
    assert numbers == list(range(1, len(parts) + 1))
    assert {p.metadata["part_count"] for p in parts} == {len(parts)}


# ------------------------------------ 4. an ordinary figure is untouched


def test_a_short_figure_is_one_chunk_with_unchanged_metadata(tokenizer, config):
    """The gold figure case, which must come out exactly as it did before the split existed."""
    source = ChunkInput.model_validate(next(c["source"] for c in GOLD if c["id"] == "figure"))
    parts = figures(Builder(source, config, tokenizer).build())
    assert len(parts) == 1
    only = parts[0]
    assert only.source_text == "Figure 1. Two original circles connected by a line."
    assert only.retrieval_text == only.source_text
    assert set(only.metadata) == {"caption", "visual_only", "image_available"}, (
        "an unsplit figure gains no part metadata, so its chunk identity does not change"
    )
    assert only.metadata["visual_only"] is False
    assert [s.role for s in only.spans] == ["SOURCE", "CAPTION"]


def test_a_figure_at_the_budget_is_not_split(tokenizer, config):
    """The boundary: a legend that exactly fits stays one chunk."""
    caption = LEGEND
    while tokenizer.count(caption) > config.retrieval_budget_tokens:
        caption = caption.rsplit(" ", 1)[0]
    assert tokenizer.count(caption) == config.retrieval_budget_tokens, (
        "the case must land exactly on the ceiling, not below it"
    )
    parts = figures(Builder(figure_source(caption), config, tokenizer).build())
    assert len(parts) == 1, "a legend within the budget must not be split"
    assert "part_number" not in parts[0].metadata


# --------------------------------- 5. nothing is truncated, silently or otherwise


def test_nothing_is_dropped_and_no_cut_lands_inside_a_token(tokenizer, config):
    """Splitting is lossless, and its boundaries are token boundaries of the source text."""
    source = figure_source(LEGEND, neighbour=NEIGHBOUR)
    elements = {e.id: e for e in source.elements}
    parts = figures(Builder(source, config, tokenizer).build())

    caption_id = source.artifacts[0].caption_id
    spans = [s for p in parts for s in p.spans if s.element_id == caption_id]
    # Contiguous and complete: no gap, no overlap, nothing past the end.
    assert spans[0].start == 0 and spans[-1].end == len(LEGEND)
    for before, after in zip(spans, spans[1:], strict=False):
        assert before.end == after.start, "a gap would be text present in no chunk"

    starts = {start for start, _ in tokenizer.offsets(LEGEND)} | {0, len(LEGEND)}
    for span in spans:
        assert span.start in starts, f"offset {span.start} is inside a token"

    emitted = "".join(elements[s.element_id].text[s.start : s.end] for p in parts for s in p.spans)
    assert LEGEND in emitted and NEIGHBOUR in emitted


def test_the_neighbour_paragraph_is_not_consumed_by_the_figure(tokenizer, config):
    """Unlike the caption, linked prose keeps its own chunk, so the split never hides it."""
    source = figure_source(LEGEND, neighbour=NEIGHBOUR)
    output = Builder(source, config, tokenizer).build()
    children = [c for c in output.chunks if c.kind in {"TEXT_CHILD", "LIST"}]
    assert any(NEIGHBOUR in c.source_text for c in children)


# ------------------------------- 6. the figure rules themselves are unchanged


def test_a_figure_without_text_is_one_chunk_and_still_warns(tokenizer, config):
    """CHUNK_FIGURE_NO_TEXT keeps its meaning: expected for a figure with no textual neighbour."""
    source = figure_source("")
    output = Builder(source, config, tokenizer).build()
    parts = figures(output)
    assert len(parts) == 1
    assert parts[0].metadata["visual_only"] is True
    assert parts[0].source_text == ""
    assert parts[0].retrieval_text == "Source figure; no source caption or explanatory text."

    _, findings, _ = validate(source, output, config, tokenizer)
    warnings = [f for f in findings if f.code == "CHUNK_FIGURE_NO_TEXT"]
    assert len(warnings) == 1
    assert warnings[0].severity == "WARNING"
    assert warnings[0].chunk_key == parts[0].key


def test_a_split_legend_is_never_reported_as_visual_only(tokenizer, config):
    source = figure_source(LEGEND)
    output = Builder(source, config, tokenizer).build()
    assert len(figures(output)) > 1
    assert all(p.metadata["visual_only"] is False for p in figures(output))
    _, findings, _ = validate(source, output, config, tokenizer)
    assert not [f for f in findings if f.code == "CHUNK_FIGURE_NO_TEXT"]


def test_every_part_is_still_a_figure_to_the_downstream_rules(tokenizer, config):
    """M6 sufficiency and M8 claim checking recognise figures by chunk type, parts included."""
    output = Builder(figure_source(LEGEND), config, tokenizer).build()
    parts = figures(output)
    assert len(parts) > 1
    for part in parts:
        assert part.kind in SUFFICIENCY_FIGURE_CHUNKS
        assert part.kind in CLAIM_FIGURE_CHUNKS


# ------------------------- 7. the validation backstop still fails a violating construction


def test_an_over_budget_figure_chunk_is_still_an_error(tokenizer):
    """If construction ever regresses, validation must still refuse the dataset.

    The dataset is built under a policy that permits the large legend and then validated against a
    stricter one, which is exactly the shape of a construction that ignored the budget.
    """
    source = figure_source(LEGEND)
    output = Builder(source, ChunkingConfig(), tokenizer).build()
    strict = ChunkingConfig(table_max_tokens=64, child_target_tokens=64, explanation_max_tokens=64)
    assert strict.retrieval_budget_tokens == 64
    _, findings, _ = validate(source, output, strict, tokenizer)
    oversized = [f for f in findings if f.code == "CHUNK_OVERSIZED"]
    figure_keys = {c.key for c in figures(output)}
    assert [f for f in oversized if f.chunk_key in figure_keys], "figure parts must be judged"
    assert all(f.severity == "ERROR" for f in oversized if f.chunk_key in figure_keys)
    assert all(
        "embedded without truncation" in f.message for f in oversized if f.chunk_key in figure_keys
    )


def test_the_error_is_decided_on_the_retrieval_body_not_the_source_text(tokenizer):
    """The second half of the defect: the source text is a lower bound on what is embedded.

    A chunk whose source fits while its retrieval body does not is unembeddable, and judging the
    source alone declared it fine.
    """
    headings = ("Section 1 " + "Posterior Fossa " * 30,)
    source = figure_source(LEGEND, headings=headings)
    config = ChunkingConfig()
    output = Builder(source, config, tokenizer).build()
    parts = figures(output)
    assert all(p.retrieval_token_count > p.token_count for p in parts)

    # A budget that the source text of a part meets but its retrieval body does not.
    body = max(p.token_count for p in parts)
    retrieval = max(p.retrieval_token_count for p in parts)
    assert body < retrieval
    strict = ChunkingConfig(
        table_max_tokens=body,
        child_target_tokens=body,
        explanation_max_tokens=body,
        parent_target_tokens=max(body, 128),
    )
    _, findings, _ = validate(source, output, strict, tokenizer)
    keys = {p.key for p in parts if p.retrieval_token_count > body}
    errors = {
        f.chunk_key for f in findings if f.code == "CHUNK_OVERSIZED" and f.severity == "ERROR"
    }
    assert keys and keys <= errors, "an over-budget retrieval body must be an error"


# ---------------------- 8. the real MedCPT tokenizer, at the boundary


def medcpt():
    from app.embeddings.medcpt import MedCPTArticleEmbedder

    return MedCPTArticleEmbedder(EmbeddingConfig(model_cache_dir=CACHE, offline=True))


def figure_input(body: str, config: EmbeddingConfig, caption: str | None = None):
    return build(
        ChunkSource(
            chunk_id=uuid4(),
            chunk_type="FIGURE_CONTEXT",
            retrieval_text=body,
            document_title="Cerebellum and Fourth Ventricle",
            caption=caption,
        ),
        config,
    )


@MODEL
def test_a_figure_input_at_the_limit_is_accepted_and_one_token_over_is_not():
    """The encoder's own boundary, measured with its own tokenizer and special tokens."""
    config = EmbeddingConfig(model_cache_dir=CACHE, offline=True)
    model = medcpt()
    word = "the "
    body = word * 700
    while True:
        measured = model.measure((figure_input(body, config),))[0]
        if measured.token_count <= config.max_input_tokens:
            break
        body = body[: -len(word)]
    assert measured.token_count == config.max_input_tokens
    assert not measured.exceeds_limit

    over = model.measure((figure_input(body + word, config),))[0]
    assert over.token_count == config.max_input_tokens + 1
    assert over.exceeds_limit


@MODEL
def test_every_emitted_figure_part_is_embeddable_by_the_real_encoder(tokenizer, config):
    """End to end: what the builder emits for the failing document, measured by MedCPT itself."""
    embedding = EmbeddingConfig(model_cache_dir=CACHE, offline=True)
    model = medcpt()
    headings = (
        "Section 1 The Cerebellum and Fourth Ventricle",
        "Chapter 1 Microsurgical Anatomy of the Posterior Fossa",
    )
    source = figure_source(LEGEND, neighbour=NEIGHBOUR, headings=headings)
    parts = figures(Builder(source, config, tokenizer).build())
    assert len(parts) > 1

    inputs = tuple(
        figure_input(part.retrieval_text, embedding, caption=part.metadata["caption"])
        for part in parts
    )
    for measurement in model.measure(inputs):
        assert not measurement.exceeds_limit, (
            f"{measurement.token_count} tokens exceeds {embedding.max_input_tokens}"
        )
