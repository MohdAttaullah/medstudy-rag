"""The chunking/embedding input contract: a retrieval unit must be embeddable by construction.

A real 932-page textbook produced one table part of 545 tokens against a 450-token ceiling. The
split budgeted `prefix + rows` while emitting `prefix + rows + suffix`, so the table's 105-token
footnote legend was never counted; the encoder then refused the 555-token input and failed the
whole embedding run two stages away from the cause.

Three things are protected here. The split budgets exactly what it emits. A configuration whose
chunk targets cannot fit the encoder is refused at startup. A retrieval unit that still cannot be
embedded fails at chunk validation, where the chunk can be named and the policy changed, rather
than at embedding.

TEXT_PARENT is exempt throughout: it is a context container M6 slices, never an embedding input.
"""

import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from app.core.chunking_config import ChunkingConfig
from app.core.config import Settings
from app.core.embedding_config import EmbeddingConfig
from app.embeddings.inputs import ChunkSource, build, eligible
from app.ingestion.chunking.builder import Builder
from app.ingestion.chunking.model import ChunkInput
from app.ingestion.chunking.quality import validate
from app.ingestion.chunking.tokenizer import LocalTokenizer
from pydantic import ValidationError

GOLD = json.loads(
    (Path(__file__).parent / "fixtures/chunking/gold.json").read_text(encoding="utf-8")
)["cases"]
CACHE = Path(".local/models/embeddings")
MODEL = pytest.mark.skipif(
    not CACHE.exists(), reason="Run scripts/provision_embedding_model.py first"
)

FOOTNOTES = (
    "AMB, Amphotericin B; ECH, echinocandins (anidulafungin, caspofungin, and micafungin); "
    "FC, flucytosine; FCZ, fluconazole; ITZ, itraconazole; KTZ, ketoconazole; VCZ, voriconazole."
)


@pytest.fixture(scope="module")
def tokenizer():
    return LocalTokenizer(ChunkingConfig())


def table_source(with_footnotes: bool) -> ChunkInput:
    """The gold large table, optionally carrying a linked footnote the way a real one does."""
    source = deepcopy(next(c["source"] for c in GOLD if c["id"] == "large-table"))
    if with_footnotes:
        artifact = source["artifacts"][0]
        anchor = next(e for e in source["elements"] if e["id"] == artifact["element_id"])
        note = deepcopy(anchor)
        note["id"] = str(uuid5(UUID(int=11), "table-footnote"))
        note["text"] = FOOTNOTES
        source["elements"].append(note)
        artifact["related_ids"] = [note["id"]]
    return ChunkInput.model_validate(source)


def table_chunks(output):
    return [c for c in output.chunks if c.kind in {"TABLE", "TABLE_PART"}]


# --------------------------------------------------------------- Fix A: the split budget


def test_the_split_budgets_the_footnotes_it_emits(tokenizer):
    """The defect itself: every emitted part must be within the ceiling it was measured against."""
    config = ChunkingConfig(table_max_tokens=120)
    output = Builder(table_source(with_footnotes=True), config, tokenizer).build()
    parts = table_chunks(output)
    assert parts, "the fixture must produce table chunks"
    for part in parts:
        assert tokenizer.count(part.source_text) <= config.table_max_tokens, (
            f"{part.kind} of {tokenizer.count(part.source_text)} tokens exceeds "
            f"{config.table_max_tokens}"
        )


def test_a_single_part_table_is_budget_checked_too(tokenizer):
    """A table small enough not to split still carries its footnotes into the emitted text."""
    config = ChunkingConfig(table_max_tokens=120)
    output = Builder(table_source(with_footnotes=True), config, tokenizer).build()
    for part in table_chunks(output):
        rendered = tokenizer.count(part.source_text)
        assert rendered <= config.table_max_tokens
        if part.kind == "TABLE":
            assert part.metadata["part_count"] == 1


def test_footnotes_push_a_table_into_more_parts(tokenizer):
    """The footnote is not free: accounting for it is what changes where the split falls."""
    config = ChunkingConfig(table_max_tokens=120)
    without = table_chunks(Builder(table_source(False), config, tokenizer).build())
    with_notes = table_chunks(Builder(table_source(True), config, tokenizer).build())
    assert len(with_notes) > len(without), (
        "a table whose parts each repeat a 100-token legend must split more finely"
    )


def test_no_row_cell_header_or_footnote_disappears(tokenizer):
    """Splitting differently must not lose content. Provenance is the point of the whole stage."""
    config = ChunkingConfig(table_max_tokens=120)
    source = table_source(with_footnotes=True)
    parts = table_chunks(Builder(source, config, tokenizer).build())
    artifact = source.artifacts[0]

    rows = [i for part in parts for i in part.metadata["row_indexes"]]
    headers = {i for part in parts for i in part.metadata["header_rows"]}
    declared = {c["row"] for c in artifact.data["cells"]}
    assert rows == sorted(rows) and len(rows) == len(set(rows)), "rows appear once, in order"
    assert set(rows) | headers == declared, "every declared row survives somewhere"
    assert headers == {0} and len({p.metadata["headers"] for p in parts}) == 1
    assert all(p.metadata["headers"] in p.source_text for p in parts), "headers repeat verbatim"
    assert all(FOOTNOTES in p.source_text for p in parts), "footnotes repeat with every part"

    # Cells are carried per part and not invented.
    carried = [c for part in parts for c in part.metadata["cells"]]
    assert len(carried) >= len(artifact.data["cells"])
    assert all(c in artifact.data["cells"] for c in carried)


def test_part_numbering_and_identity_stay_coherent(tokenizer):
    config = ChunkingConfig(table_max_tokens=120)
    parts = table_chunks(Builder(table_source(True), config, tokenizer).build())
    assert [p.metadata["part_number"] for p in parts] == list(range(1, len(parts) + 1))
    assert {p.metadata["part_count"] for p in parts} == {len(parts)}
    assert len({p.key for p in parts}) == len(parts), "content identity stays unique"


def test_the_split_is_deterministic(tokenizer):
    """Same source and policy, same boundaries and same identities, every time."""
    config = ChunkingConfig(table_max_tokens=120)
    first = table_chunks(Builder(table_source(True), config, tokenizer).build())
    second = table_chunks(Builder(table_source(True), config, tokenizer).build())
    assert [c.key for c in first] == [c.key for c in second]
    assert [c.metadata["row_indexes"] for c in first] == [c.metadata["row_indexes"] for c in second]


def test_a_table_without_footnotes_is_unaffected(tokenizer):
    """The fix must not move boundaries for tables that never had a suffix."""
    config = ChunkingConfig(table_max_tokens=120)
    parts = table_chunks(Builder(table_source(False), config, tokenizer).build())
    for part in parts:
        assert tokenizer.count(part.source_text) <= config.table_max_tokens


# ------------------------------------------------- Fix B: the configuration-time contract


def test_the_shipped_policy_fits_the_encoder():
    settings = Settings()
    budget = settings.chunking.retrieval_budget_tokens
    reserve = settings.embedding.context_token_reserve
    assert budget == 384
    assert reserve == 128
    assert budget + reserve == settings.embedding.max_input_tokens == 512


def test_the_retrieval_budget_is_derived_from_the_targets():
    config = ChunkingConfig(table_max_tokens=384)
    assert config.retrieval_budget_tokens == max(
        config.table_max_tokens, config.child_target_tokens, config.explanation_max_tokens
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"table_max_tokens": 450},  # the value that produced the real failure
        {"child_target_tokens": 500},
        {"explanation_max_tokens": 400},
    ],
)
def test_a_policy_that_cannot_be_embedded_is_refused_at_startup(overrides):
    """Unrepresentable by construction, rather than discovered by a large enough table."""
    with pytest.raises(ValidationError, match="cannot fit the embedding input"):
        Settings(chunking=ChunkingConfig(**overrides))


def test_a_parent_target_far_above_the_encoder_limit_stays_legal():
    """Parents are context containers; the embedding contract does not apply to them."""
    settings = Settings(chunking=ChunkingConfig(parent_target_tokens=1280))
    assert settings.chunking.parent_target_tokens > settings.embedding.max_input_tokens
    assert not eligible("TEXT_PARENT", settings.embedding)


def test_a_larger_reserve_narrows_what_chunking_may_emit():
    with pytest.raises(ValidationError, match="cannot fit the embedding input"):
        Settings(embedding=EmbeddingConfig(context_token_reserve=256))


# ------------------------------------------------- Fix C: failing at chunking, not embedding


def test_an_unembeddable_retrieval_chunk_is_an_error_not_a_warning(tokenizer):
    """This is what should have stopped the real book at M3 instead of M4.

    The dataset that failed embedding carried exactly this finding — at WARNING, so it passed.
    """
    config = ChunkingConfig(table_max_tokens=384)
    source = table_source(with_footnotes=True)
    output = Builder(source, config, tokenizer).build()
    # Validate against a policy whose budget is below what was actually emitted.
    strict = ChunkingConfig(table_max_tokens=64, child_target_tokens=64, explanation_max_tokens=64)
    _, findings, _ = validate(source, output, strict, tokenizer)
    oversized = [f for f in findings if f.code == "CHUNK_OVERSIZED"]
    assert oversized, "an over-budget retrieval unit must be reported"
    assert all(f.severity == "ERROR" for f in oversized)
    assert all("embedded without truncation" in f.message for f in oversized)


def test_a_parent_over_the_retrieval_budget_is_never_an_error(tokenizer):
    """The exemption that keeps 618 parents of a real textbook legal.

    Validated under a policy whose retrieval budget is 32 tokens, every parent here is far over
    it. None may be an error: a parent is a context container M6 slices, never an embedding input.
    Reporting it against its own target is still useful, so that stays a warning.
    """
    source = ChunkInput.model_validate(next(c["source"] for c in GOLD if c["id"] == "paragraphs"))
    output = Builder(source, ChunkingConfig(), tokenizer).build()
    strict = ChunkingConfig(
        table_max_tokens=32,
        child_target_tokens=32,
        explanation_max_tokens=32,
        parent_target_tokens=128,
    )
    assert strict.retrieval_budget_tokens == 32
    parents = {c.key: c for c in output.chunks if c.kind == "TEXT_PARENT"}
    assert parents, "the fixture must produce parents"
    assert any(c.token_count > strict.retrieval_budget_tokens for c in parents.values()), (
        "the case is only meaningful if a parent exceeds the retrieval budget"
    )

    _, findings, _ = validate(source, output, strict, tokenizer)
    oversized = [f for f in findings if f.code == "CHUNK_OVERSIZED"]
    assert not [f for f in oversized if f.chunk_key in parents and f.severity == "ERROR"]
    assert all(f.severity == "ERROR" for f in oversized if f.chunk_key not in parents), (
        "retrieval units over the budget are errors; only parents are exempt"
    )


def test_a_compliant_dataset_raises_no_oversized_error(tokenizer):
    config = ChunkingConfig()
    source = table_source(with_footnotes=True)
    output = Builder(source, config, tokenizer).build()
    _, findings, _ = validate(source, output, config, tokenizer)
    errors = [f for f in findings if f.code == "CHUNK_OVERSIZED" and f.severity == "ERROR"]
    assert not errors


# ------------------------------- the real encoder: the contract measured, not assumed


def medcpt():
    from app.embeddings.medcpt import MedCPTArticleEmbedder

    return MedCPTArticleEmbedder(EmbeddingConfig(model_cache_dir=CACHE, offline=True))


def measured(body: str, hierarchy: tuple[str, ...] = (), caption: str | None = None) -> int:
    config = EmbeddingConfig(model_cache_dir=CACHE, offline=True)
    source = ChunkSource(
        chunk_id=uuid4(),
        chunk_type="TABLE_PART",
        retrieval_text=body,
        document_title="Medical Microbiology",
        hierarchy=hierarchy,
        caption=caption,
    )
    return medcpt().measure((build(source, config),))[0].token_count


@MODEL
def test_the_reserve_covers_a_real_context_field(tokenizer):
    """The reserve is a claim about hierarchy and caption overhead; measure it, do not assume it.

    Measured with MedCPT's own tokenizer, including its special tokens and both input fields.
    """
    body = " ".join(["Candida albicans susceptibility value"] * 40)
    bare = measured(body)
    rich = measured(
        body,
        hierarchy=("Section 3 Basic Concepts", "Chapter 61 Antifungal Agents", "Susceptibility"),
        caption="Table 61-3. In vitro activity of antifungal agents against selected fungi.",
    )
    assert rich > bare, "context genuinely costs tokens"
    assert rich - bare < EmbeddingConfig().context_token_reserve, (
        "a full hierarchy and caption must fit inside the reserved allowance"
    )


@MODEL
def test_an_input_at_the_limit_is_accepted_and_one_token_over_is_not(tokenizer):
    config = EmbeddingConfig(model_cache_dir=CACHE, offline=True)
    model = medcpt()
    # A single-token word, so removing one word removes exactly one token and the walk can land
    # on the limit rather than stepping over it.
    word = "the "
    body = word * 700
    # Walk down to an input of exactly the limit, then add one token.
    while True:
        source = ChunkSource(
            chunk_id=uuid4(),
            chunk_type="TABLE_PART",
            retrieval_text=body,
            document_title="Medical Microbiology",
        )
        count = model.measure((build(source, config),))[0].token_count
        if count <= config.max_input_tokens:
            break
        body = body[: -len(word)]
    assert count == config.max_input_tokens, f"expected exactly {config.max_input_tokens}"
    assert not model.measure((build(source, config),))[0].exceeds_limit

    over = ChunkSource(
        chunk_id=uuid4(),
        chunk_type="TABLE_PART",
        retrieval_text=body + word,
        document_title="Medical Microbiology",
    )
    assert model.measure((build(over, config),))[0].exceeds_limit


@MODEL
def test_a_body_within_the_retrieval_budget_always_fits_the_encoder(tokenizer):
    """The contract end to end: chunking's ceiling plus a real context field stays under 512."""
    config = EmbeddingConfig(model_cache_dir=CACHE, offline=True)
    budget = ChunkingConfig().retrieval_budget_tokens
    body = " ".join(["Cryptococcus neoformans moderate activity"] * 200)
    while LocalTokenizer(ChunkingConfig()).count(body) > budget:
        body = body.rsplit(" ", 1)[0]
    assert LocalTokenizer(ChunkingConfig()).count(body) <= budget

    count = measured(
        body,
        hierarchy=("Section 3 Basic Concepts", "Chapter 61 Antifungal Agents", "Susceptibility"),
        caption="Table 61-3. In vitro activity of antifungal agents against selected fungi.",
    )
    assert count <= config.max_input_tokens, f"{count} tokens exceeds the encoder limit"


@MODEL
def test_parents_are_never_measured_against_the_encoder_limit():
    """A 1280-token parent is legal and simply never becomes an input."""
    config = EmbeddingConfig(model_cache_dir=CACHE, offline=True)
    assert not eligible("TEXT_PARENT", config)
    for kind in config.eligible_chunk_types:
        assert eligible(kind, config)
