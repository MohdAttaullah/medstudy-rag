"""M5 unit tests: analyzer, BM25 arithmetic, fusion, metrics, query policy and the state boundary.

Nothing here touches PostgreSQL, Qdrant or the network. The BM25 and RRF assertions are against
values computed by hand from the published formulae, so a refactor that changes ranking behaviour
fails here rather than showing up as an unexplained shift in an evaluation number.

The tests that need the real query encoder are at the end and skip when the pinned model has not
been provisioned; they assert the representation itself, which is the one thing a stub cannot.
"""

import math
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.core.embedding_config import EmbeddingConfig
from app.core.errors import DomainError
from app.core.retrieval_config import (
    QueryEncoderConfig,
    RetrievalConfig,
    SparseAnalyzerConfig,
    SparseIndexConfig,
)
from app.evaluation.retrieval import (
    GoldCase,
    classify,
    fixture_id,
    load_gold,
    ndcg_at,
    precision_at,
    recall_at,
    reciprocal_rank,
)
from app.ingestion.state import REINDEX_SPARSE_ORIGINS, TRANSITIONS, require_transition
from app.models.enums import Status
from app.retrieval.errors import MESSAGES, RetrievalError, retryable
from app.retrieval.fusion.rrf import ReciprocalRankFusion
from app.retrieval.model import LaneHit, Ranking, RetrievalFilters
from app.retrieval.query.normalize import normalize, query_hash
from app.retrieval.sparse.analyzer import frequencies, query_terms, terms
from app.retrieval.sparse.bm25 import (
    CorpusStatistics,
    InMemorySparseIndex,
    Posting,
    inverse_document_frequency,
    rank,
    term_score,
)
from app.retrieval.validation import MESSAGES as FINDING_MESSAGES

GOLD = Path(__file__).parent / "fixtures/retrieval/gold.json"
ANALYZER = SparseAnalyzerConfig()


# --------------------------------------------------------------------------------- analyzer


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HLA-B27 positive", "hla-b27"),
        ("CYP3A4 inhibitor", "cyp3a4"),
        ("Na+/K+-ATPase pump", "na+/k+-atpase"),
        ("HbA1c result", "hba1c"),
        ("IL-6 level", "il-6"),
        ("HER2/neu status", "her2/neu"),
        ("25-hydroxyvitamin D", "25-hydroxyvitamin"),
        ("dose 1.5 mg/kg", "mg/kg"),
        ("a value of 7.5% today", "7.5%"),
        ("BRCA1 variant", "brca1"),
    ],
)
def test_biomedical_identifiers_survive_analysis(text, expected):
    """The whole point of the lexical lane: an identifier must remain one term."""
    assert expected in query_terms(text, ANALYZER)


def test_compound_identifiers_also_yield_their_letter_bearing_parts():
    found = query_terms("HLA-B27", ANALYZER)
    assert "hla-b27" in found and "hla" in found and "b27" in found


def test_single_character_fragments_are_not_emitted_as_parts():
    # `il-6` keeps the whole identifier; a bare "6" would match almost every numeric passage.
    found = query_terms("IL-6", ANALYZER)
    assert "il-6" in found and "il" in found and "6" not in found


def test_case_exact_terms_occupy_a_separate_key_space():
    found = query_terms("HLA-B27", ANALYZER)
    assert "^HLA-B27" in found
    # A lowercase query produces no case-exact term, so the exact form is a genuinely rarer signal.
    assert not [term for term in query_terms("hla-b27", ANALYZER) if term.startswith("^")]


def test_case_policy_normalized_only_drops_the_exact_lane():
    plain = SparseAnalyzerConfig(case_policy="NORMALIZED")
    assert not [term for term in query_terms("HLA-B27", plain) if term.startswith("^")]
    assert plain.analyzer_fingerprint != ANALYZER.analyzer_fingerprint


def test_compound_policy_whole_only_drops_the_parts():
    whole = SparseAnalyzerConfig(compound_policy="WHOLE")
    found = query_terms("HLA-B27", whole)
    assert "hla-b27" in found and "hla" not in found


def test_sentence_punctuation_is_not_part_of_a_term():
    assert "diabetes" in query_terms("Consider diabetes.", ANALYZER)
    assert "diabetes." not in query_terms("Consider diabetes.", ANALYZER)


def test_typographic_characters_fold_to_their_ascii_equivalents():
    curly = query_terms("the patient’s 25–hydroxyvitamin D", ANALYZER)
    straight = query_terms("the patient's 25-hydroxyvitamin D", ANALYZER)
    assert curly == straight


def test_stopwords_are_disabled_by_default():
    """A medical question is short; discarding its ordinary words discards relationships."""
    assert ANALYZER.stopwords == ()
    assert "with" in query_terms("associated with ankylosing spondylitis", ANALYZER)


def test_configured_stopwords_are_removed_and_change_the_analyzer_identity():
    filtered = SparseAnalyzerConfig(stopwords=("with",))
    assert "with" not in query_terms("associated with spondylitis", filtered)
    assert filtered.analyzer_fingerprint != ANALYZER.analyzer_fingerprint


def test_query_terms_are_distinct_and_ordered_by_first_appearance():
    assert query_terms("fever fever chills", ANALYZER) == ("fever", "chills")


def test_frequencies_count_the_emitted_stream_not_the_surface_words():
    counts, length = frequencies("HLA-B27 HLA-B27", ANALYZER)
    assert counts["hla-b27"] == 2
    assert length == len(terms("HLA-B27 HLA-B27", ANALYZER))


def test_analysis_is_deterministic():
    text = "Digoxin inhibits the Na+/K+-ATPase pump; HbA1c was 7.5%."
    assert terms(text, ANALYZER) == terms(text, ANALYZER)


def test_term_length_bounds_are_enforced():
    long_token = "a" * 200
    assert not [term for term in query_terms(long_token, ANALYZER) if len(term) > 64]


def test_analyzer_fingerprint_excludes_nothing_that_changes_the_term_set():
    for changed in (
        SparseAnalyzerConfig(unicode_normalization="NFKC", min_term_length=2),
        SparseAnalyzerConfig(max_term_length=32),
        SparseAnalyzerConfig(case_policy="NORMALIZED"),
        SparseAnalyzerConfig(compound_policy="WHOLE"),
    ):
        assert changed.analyzer_fingerprint != ANALYZER.analyzer_fingerprint


def test_analyzer_fingerprint_ignores_settings_that_do_not_change_terms():
    """A larger batch does not change what a term is and must not fragment the vocabulary."""
    assert (
        SparseAnalyzerConfig(
            version="sparse-analyzer-m5-v1", max_terms_per_chunk=999
        ).analyzer_fingerprint
        == ANALYZER.analyzer_fingerprint
    )


# ------------------------------------------------------------------------------------- BM25


def test_inverse_document_frequency_matches_the_lucene_formula():
    assert inverse_document_frequency(1, 100) == pytest.approx(math.log(1 + 99.5 / 1.5))
    assert inverse_document_frequency(50, 100) == pytest.approx(math.log(1 + 50.5 / 50.5))


def test_inverse_document_frequency_is_never_negative():
    """A document must never be penalised for containing a query term."""
    for frequency in (1, 50, 99, 100):
        assert inverse_document_frequency(frequency, 100) >= 0


def test_a_rare_term_outweighs_a_common_one():
    assert inverse_document_frequency(1, 100) > inverse_document_frequency(80, 100)


def test_term_score_matches_a_hand_computed_value():
    idf, tf, length, average, k1, b = 2.0, 3, 100, 80.0, 1.2, 0.75
    expected = idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / average))
    assert term_score(tf, length, idf, k1=k1, b=b, average_length=average) == pytest.approx(
        expected
    )


def test_term_frequency_saturates():
    """Doubling occurrences must not double the score; that is what k1 controls."""
    single = term_score(1, 50, 1.0, k1=1.2, b=0.75, average_length=50)
    many = term_score(10, 50, 1.0, k1=1.2, b=0.75, average_length=50)
    assert many < 10 * single


def test_length_normalization_penalises_a_longer_document():
    short = term_score(2, 25, 1.0, k1=1.2, b=0.75, average_length=50)
    long = term_score(2, 200, 1.0, k1=1.2, b=0.75, average_length=50)
    assert short > long


def test_b_zero_disables_length_normalization():
    short = term_score(2, 25, 1.0, k1=1.2, b=0.0, average_length=50)
    long = term_score(2, 200, 1.0, k1=1.2, b=0.0, average_length=50)
    assert short == pytest.approx(long)


def _postings(*rows):
    return [
        Posting(chunk_id=chunk, term=term, term_frequency=frequency, length=length)
        for chunk, term, frequency, length in rows
    ]


def test_ranking_orders_by_total_score_and_reports_matched_terms():
    first, second = uuid4(), uuid4()
    statistics = CorpusStatistics(
        document_count=10, total_length=1000, document_frequency={"alpha": 1, "beta": 5}
    )
    scored = rank(
        _postings((first, "alpha", 2, 100), (first, "beta", 1, 100), (second, "beta", 3, 100)),
        statistics,
        ("alpha", "beta"),
        k1=1.2,
        b=0.75,
        top_k=10,
    )
    assert [chunk for chunk, _, _ in scored] == [first, second]
    assert scored[0][2] == ("alpha", "beta")


def test_ties_are_broken_deterministically_by_chunk_id():
    left, right = sorted((uuid4(), uuid4()), key=str)
    statistics = CorpusStatistics(
        document_count=10, total_length=1000, document_frequency={"alpha": 2}
    )
    scored = rank(
        _postings((right, "alpha", 1, 100), (left, "alpha", 1, 100)),
        statistics,
        ("alpha",),
        k1=1.2,
        b=0.75,
        top_k=10,
    )
    assert [chunk for chunk, _, _ in scored] == [left, right]


def test_ranking_ignores_terms_the_query_did_not_ask_for():
    chunk = uuid4()
    statistics = CorpusStatistics(
        document_count=10, total_length=1000, document_frequency={"alpha": 1, "gamma": 1}
    )
    scored = rank(
        _postings((chunk, "gamma", 9, 100)),
        statistics,
        ("alpha",),
        k1=1.2,
        b=0.75,
        top_k=10,
    )
    assert scored == ()


def test_filters_exclude_candidates_before_scoring():
    chunk = uuid4()
    statistics = CorpusStatistics(
        document_count=10, total_length=1000, document_frequency={"alpha": 1}
    )
    postings = [
        Posting(
            chunk_id=chunk,
            term="alpha",
            term_frequency=2,
            length=100,
            source_type="QUESTION_BANK",
        )
    ]
    assert rank(postings, statistics, ("alpha",), k1=1.2, b=0.75, top_k=5) != ()
    assert (
        rank(
            postings,
            statistics,
            ("alpha",),
            k1=1.2,
            b=0.75,
            top_k=5,
            filters=RetrievalFilters(source_types=("TEXTBOOK",)),
        )
        == ()
    )


def test_in_memory_index_finds_an_exact_identifier_over_a_near_neighbour():
    index = InMemorySparseIndex(config=ANALYZER)
    target, neighbour = uuid4(), uuid4()
    index.add(target, "HLA-B27 is associated with ankylosing spondylitis.")
    index.add(neighbour, "HLA-DR4 is associated with rheumatoid arthritis.")
    for filler in range(20):
        index.add(uuid4(), f"An unrelated passage about topic number {filler}.")
    ranking = index.search("HLA-B27", top_k=5)
    assert ranking.hits[0].chunk_id == target


def test_empty_query_terms_return_no_candidates_rather_than_everything():
    index = InMemorySparseIndex(config=ANALYZER)
    index.add(uuid4(), "Some content.")
    assert index.search("!!! ???", top_k=5).hits == ()


# -------------------------------------------------------------------------------------- RRF


def _ranking(lane, chunks, weight=1.0):
    return Ranking(
        lane=lane,
        hits=tuple(
            LaneHit(chunk_id=chunk, score=100.0 - position, rank=position)
            for position, chunk in enumerate(chunks, start=1)
        ),
        duration_ms=0.0,
        weight=weight,
    )


def test_a_candidate_in_both_lanes_rises_above_one_that_is_only_in_a_single_lane():
    both, dense_only, sparse_only = uuid4(), uuid4(), uuid4()
    fused = ReciprocalRankFusion(60).fuse(
        [_ranking("DENSE", [dense_only, both]), _ranking("BM25", [sparse_only, both])], 10
    )
    assert fused[0].chunk_id == both
    assert fused[0].lanes == ("DENSE", "BM25")
    assert fused[0].fused_score == pytest.approx(1 / 62 + 1 / 62)


def test_a_candidate_from_one_lane_only_still_survives_fusion():
    dense_only, sparse_only = uuid4(), uuid4()
    fused = ReciprocalRankFusion(60).fuse(
        [_ranking("DENSE", [dense_only]), _ranking("BM25", [sparse_only])], 10
    )
    assert {hit.chunk_id for hit in fused} == {dense_only, sparse_only}
    assert all(len(hit.lanes) == 1 for hit in fused)


def test_fusion_records_each_lane_rank_and_score():
    chunk = uuid4()
    fused = ReciprocalRankFusion(60).fuse(
        [_ranking("DENSE", [chunk]), _ranking("BM25", [uuid4(), chunk])], 10
    )
    hit = next(item for item in fused if item.chunk_id == chunk)
    assert hit.dense_rank == 1 and hit.sparse_rank == 2
    assert hit.dense_score == 99.0 and hit.sparse_score == 98.0


def test_a_duplicate_candidate_within_one_lane_is_counted_once():
    chunk = uuid4()
    duplicated = Ranking(
        lane="DENSE",
        hits=(
            LaneHit(chunk_id=chunk, score=9.0, rank=1),
            LaneHit(chunk_id=chunk, score=8.0, rank=2),
        ),
        duration_ms=0.0,
    )
    fused = ReciprocalRankFusion(60).fuse([duplicated], 10)
    assert len(fused) == 1
    assert fused[0].fused_score == pytest.approx(1 / 61)
    assert fused[0].dense_rank == 1


def test_fusion_ties_are_deterministic():
    left, right = sorted((uuid4(), uuid4()), key=str)
    first = ReciprocalRankFusion(60).fuse(
        [_ranking("DENSE", [right]), _ranking("BM25", [left])], 10
    )
    second = ReciprocalRankFusion(60).fuse(
        [_ranking("DENSE", [right]), _ranking("BM25", [left])], 10
    )
    assert [hit.chunk_id for hit in first] == [hit.chunk_id for hit in second] == [left, right]


def test_weights_shift_the_ordering_between_lanes():
    dense_only, sparse_only = uuid4(), uuid4()
    weighted = ReciprocalRankFusion(60).fuse(
        [
            _ranking("DENSE", [dense_only], weight=2.0),
            _ranking("BM25", [sparse_only], weight=1.0),
        ],
        10,
    )
    assert weighted[0].chunk_id == dense_only


def test_a_smaller_constant_sharpens_the_influence_of_the_top_rank():
    chunks = [uuid4() for _ in range(3)]
    sharp = ReciprocalRankFusion(1).fuse([_ranking("DENSE", chunks)], 10)
    flat = ReciprocalRankFusion(1000).fuse([_ranking("DENSE", chunks)], 10)
    assert sharp[0].fused_score / sharp[1].fused_score > flat[0].fused_score / flat[1].fused_score


def test_fusion_respects_the_final_candidate_limit():
    chunks = [uuid4() for _ in range(30)]
    assert len(ReciprocalRankFusion(60).fuse([_ranking("DENSE", chunks)], 5)) == 5


def test_fusion_constant_must_be_positive():
    with pytest.raises(ValueError):
        ReciprocalRankFusion(0)


# ---------------------------------------------------------------------------------- metrics


def _case(relevant, graded=False):
    return GoldCase(id="c", category="x", query="q", relevant=relevant, graded=graded)


def test_recall_counts_every_relevant_chunk():
    first, second = fixture_id("a"), fixture_id("b")
    ranked = [first, uuid4(), second]
    relevant = {first, second}
    assert recall_at(ranked, relevant, 1) == 0.5
    assert recall_at(ranked, relevant, 3) == 1.0


def test_recall_is_undefined_without_relevant_evidence():
    """A negative case has no recall; inventing 0.0 or 1.0 would move the headline figure."""
    with pytest.raises(ValueError):
        recall_at([uuid4()], set(), 5)


def test_reciprocal_rank_uses_the_first_relevant_position():
    first, second = fixture_id("a"), fixture_id("b")
    assert reciprocal_rank([uuid4(), first, second], {first, second}) == pytest.approx(0.5)
    assert reciprocal_rank([uuid4()], {first}) == 0.0


def test_precision_measures_the_window_not_the_gold_set():
    relevant = {fixture_id("a")}
    assert precision_at([fixture_id("a"), uuid4()], relevant, 2) == 0.5


def test_ndcg_rewards_placing_the_higher_graded_chunk_first():
    case = _case({"a": 3, "b": 1}, graded=True)
    good = ndcg_at([fixture_id("a"), fixture_id("b")], case, 5)
    poor = ndcg_at([fixture_id("b"), fixture_id("a")], case, 5)
    assert good == pytest.approx(1.0)
    assert poor < good


def test_ndcg_is_zero_when_nothing_relevant_was_retrieved():
    assert ndcg_at([uuid4(), uuid4()], _case({"a": 3}, graded=True), 5) == 0.0


def test_failure_classification_distinguishes_the_lanes():
    case = _case({"a": 3})
    target = fixture_id("a")
    corpus = {target}
    # Found by dense and missed by the lexical lane: the mismatch is a lexical one.
    assert classify(case, [target], [], [target], corpus) == "LEXICAL_MISMATCH"
    # Found lexically and missed by dense: the mismatch is a semantic one.
    assert classify(case, [], [target], [target], corpus) == "SEMANTIC_MISMATCH"
    assert classify(case, [], [], [], corpus) == "BOTH_LANES_MISSED"
    assert classify(case, [target], [], [], corpus) == "FUSION_DISPLACED"
    assert classify(case, [], [], [], set()) == "CORPUS_LACKS_EVIDENCE"


def test_gold_dataset_is_internally_consistent():
    gold = load_gold(GOLD)
    labels = {chunk.label for chunk in gold.chunks}
    documents = {document.label for document in gold.documents}
    assert gold.notice.startswith("SYNTHETIC")
    assert len(labels) == len(gold.chunks)
    for chunk in gold.chunks:
        assert chunk.document in documents
        assert chunk.page_start >= 1 and chunk.page_end >= chunk.page_start
        assert chunk.text.strip()
    identifiers = {case.id for case in gold.cases}
    assert len(identifiers) == len(gold.cases)
    for case in gold.cases:
        assert case.query.strip()
        for label in case.relevant:
            assert label in labels, f"{case.id} references unknown chunk {label}"
        if case.graded:
            assert all(grade in (1, 2, 3) for grade in case.relevant.values())


def test_gold_dataset_covers_the_required_retrieval_categories():
    gold = load_gold(GOLD)
    categories = {case.category for case in gold.cases}
    for required in (
        "factual",
        "exact_drug_name",
        "gene_biomarker",
        "abbreviation",
        "synonym_wording",
        "table",
        "formula",
        "figure",
        "long_question",
        "short_keyword",
        "question_bank",
        "answer_key",
        "cross_page",
        "competing_topics",
        "negative",
        "rare_terminology",
        "numeric_units",
        "multiple_relevant",
        "authority_conflict",
        "lexically_weak",
    ):
        assert required in categories, required


def test_gold_dataset_has_negative_and_over_limit_cases():
    gold = load_gold(GOLD)
    assert [case for case in gold.cases if case.negative and not case.expect_error]
    assert [case for case in gold.cases if case.expect_error == "QUERY_TOO_LONG"]


# ------------------------------------------------------------------------- query preparation


def test_normalization_collapses_whitespace_and_control_characters():
    config = QueryEncoderConfig()
    assert normalize("  what \t is\n\n  sepsis ?  ", config) == "what is sepsis ?"


def test_normalization_preserves_clinically_meaningful_symbols():
    config = QueryEncoderConfig()
    text = "Na+/K+-ATPase, HbA1c >= 7.5%, 1.5 mg/kg"
    assert normalize(text, config) == text


def test_normalization_folds_typographic_punctuation():
    config = QueryEncoderConfig()
    assert normalize("the patient’s “dose”", config) == 'the patient\'s "dose"'


def test_normalization_never_rewrites_or_expands():
    """No abbreviation expansion, no paraphrase, no generated alternative query."""
    config = QueryEncoderConfig()
    assert normalize("MI treatment", config) == "MI treatment"


def test_query_hash_is_stable_and_encoder_scoped():
    config = QueryEncoderConfig()
    assert query_hash("sepsis", config) == query_hash("sepsis", config)
    assert query_hash("sepsis", config) != query_hash("Sepsis", config)


# ----------------------------------------------------------------------------- configuration


def test_query_encoder_is_pinned_to_the_released_query_side_model():
    config = QueryEncoderConfig()
    assert config.model_id == "ncbi/MedCPT-Query-Encoder"
    assert config.model_revision == "d83a36cc6b8e3a5c5e9d9d6ba156808c1643dcbc"
    assert config.pooling_strategy == "CLS" and config.normalization == "NONE"
    assert config.embedding_dimension == 768 and config.distance_metric == "DOT"
    assert config.truncation_policy == "REJECT"


def test_the_article_encoder_cannot_be_configured_as_the_query_encoder():
    """Encoding a question with the article encoder would put it in the wrong half of the pair."""
    with pytest.raises(ValueError):
        QueryEncoderConfig(model_id="ncbi/MedCPT-Article-Encoder")  # type: ignore[arg-type]


def test_query_vector_semantics_cannot_be_changed_by_configuration():
    for field, value in (
        ("pooling_strategy", "MEAN"),
        ("normalization", "L2"),
        ("distance_metric", "COSINE"),
        ("embedding_dimension", 384),
        ("truncation_policy", "TRUNCATE"),
    ):
        with pytest.raises(ValueError):
            QueryEncoderConfig(**{field: value})  # type: ignore[arg-type]


def test_query_and_article_encoders_are_in_the_same_vector_space():
    assert QueryEncoderConfig().incompatibility(EmbeddingConfig()) is None


def test_settings_reject_incompatible_encoder_pairs_at_startup():
    """The check runs when settings are built, not when the first question is asked."""
    assert Settings(database_url="", redis_url="").query_encoder.embedding_dimension == 768


def test_semantics_fingerprint_ignores_resource_settings():
    assert (
        QueryEncoderConfig(batch_size=1, cache_size=0).semantics_fingerprint
        == QueryEncoderConfig().semantics_fingerprint
    )


def test_semantics_fingerprint_changes_with_the_query_length_limit():
    assert (
        QueryEncoderConfig(max_query_tokens=128).semantics_fingerprint
        != QueryEncoderConfig().semantics_fingerprint
    )


def test_retrieval_config_defaults_are_declared_seeds_not_calibrated_values():
    config = RetrievalConfig()
    assert config.mode == "HYBRID_RRF"
    assert config.rrf_k == 60 and config.dense_top_k == 40 and config.final_top_k == 20
    assert config.degradation_policy == "FAIL_CLOSED"
    assert config.dense_weight == 1.0 and config.sparse_weight == 1.0


def test_retrieval_config_rejects_a_final_budget_larger_than_the_lanes_supply():
    with pytest.raises(ValueError):
        RetrievalConfig(dense_top_k=1, sparse_top_k=1, final_top_k=5)


def test_retrieval_config_rejects_hybrid_with_both_lanes_unweighted():
    with pytest.raises(ValueError):
        RetrievalConfig(mode="HYBRID_RRF", dense_weight=0, sparse_weight=0)


def test_change_classes_are_declared_on_the_fields():
    schema = RetrievalConfig.model_json_schema()["properties"]
    assert schema["dense_top_k"]["change_class"] == "runtime-safe"
    assert schema["bm25_k1"]["change_class"] == "runtime-safe"
    assert schema["rrf_k"]["change_class"] == "runtime-safe"


def test_bm25_parameters_are_runtime_safe_and_not_part_of_the_analyzer_identity():
    """Postings store raw frequencies, so k1 and b re-rank without rebuilding anything."""
    assert "bm25_k1" not in SparseAnalyzerConfig().analyzer_semantics
    assert "bm25_b" not in SparseAnalyzerConfig().analyzer_semantics


def test_sparse_analyzer_forbids_uncontrolled_expansion():
    assert SparseAnalyzerConfig().expansion == "NONE"
    with pytest.raises(ValueError):
        SparseAnalyzerConfig(expansion="SYNONYMS")  # type: ignore[arg-type]


def test_sparse_index_lease_must_outlast_its_timeout():
    with pytest.raises(ValueError):
        SparseIndexConfig(timeout_seconds=600, lease_seconds=600)


# --------------------------------------------------------------------------- error vocabulary


def test_every_declared_retrieval_error_has_a_safe_message_and_a_retry_decision():
    for code, (message, is_retryable) in MESSAGES.items():
        assert message and message[0].isupper() and message.endswith(".")
        assert isinstance(is_retryable, bool)
        assert retryable(code) is is_retryable


def test_deterministic_failures_are_not_retryable():
    for code in (
        "QUERY_ENCODER_REVISION_MISMATCH",
        "QUERY_ENCODER_CHECKSUM_MISMATCH",
        "QUERY_TOO_LONG",
        "SPARSE_INDEX_VERSION_MISMATCH",
        "RETRIEVAL_CORPUS_MISALIGNED",
        "RETRIEVAL_VECTOR_SPACE_MISMATCH",
    ):
        assert retryable(code) is False


def test_retrieval_errors_are_domain_errors_carrying_their_retryability():
    error = RetrievalError("QUERY_TOO_LONG", {"limit": 64})
    assert isinstance(error, DomainError)
    assert error.details["retryable"] is False and error.details["limit"] == 64


def test_every_sparse_finding_code_has_a_message():
    assert all(message for message in FINDING_MESSAGES.values())


# -------------------------------------------------------------------------- the M5 boundary


def test_the_pipeline_ends_at_retrieval_ready():
    assert TRANSITIONS[Status.VERIFYING_SPARSE_INDEX] >= {Status.RETRIEVAL_READY}
    assert TRANSITIONS[Status.RETRIEVAL_READY] == {Status.CANCELLED}


def test_ready_remains_unreachable():
    """READY would mean the corpus is answerable. M5 makes it searchable, which is not the same."""
    assert all(Status.READY not in targets for targets in TRANSITIONS.values())
    assert Status.READY not in TRANSITIONS


def test_ready_for_retrieval_now_continues_into_the_lexical_stages():
    assert Status.SPARSE_INDEXING in TRANSITIONS[Status.READY_FOR_RETRIEVAL]
    assert TRANSITIONS[Status.SPARSE_INDEXING] >= {Status.VERIFYING_SPARSE_INDEX}


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (Status.READY_FOR_RETRIEVAL, Status.RETRIEVAL_READY),
        (Status.SPARSE_INDEXING, Status.RETRIEVAL_READY),
        (Status.VERIFYING_SPARSE_INDEX, Status.READY),
        (Status.RETRIEVAL_READY, Status.READY),
        (Status.RETRIEVAL_READY, Status.SPARSE_INDEXING),
        (Status.EMBEDDING, Status.RETRIEVAL_READY),
    ],
)
def test_rejected_transitions(current, target):
    with pytest.raises(DomainError):
        require_transition(current, target)


def test_lexical_rebuild_is_explicit_and_bounded():
    assert REINDEX_SPARSE_ORIGINS == {Status.RETRIEVAL_READY, Status.NEEDS_REVIEW, Status.FAILED}
    require_transition(Status.RETRIEVAL_READY, Status.READY_FOR_RETRIEVAL, resparse=True)
    with pytest.raises(DomainError):
        require_transition(Status.EMBEDDING, Status.READY_FOR_RETRIEVAL, resparse=True)


def test_reprocessing_paths_still_accept_a_retrieval_ready_job():
    require_transition(Status.RETRIEVAL_READY, Status.VALIDATING, reparse=True)
    require_transition(Status.RETRIEVAL_READY, Status.READY_FOR_CHUNKING, rechunk=True)
    require_transition(Status.RETRIEVAL_READY, Status.READY_FOR_EMBEDDING, reembed=True)


# ------------------------------------------------------------------- the real query encoder

CACHE = Path(".local/models/embeddings")
encoder_available = pytest.mark.skipif(
    not (CACHE / "models--ncbi--MedCPT-Query-Encoder").exists(),
    reason="Run scripts/provision_embedding_model.py --model query first",
)


@pytest.fixture(scope="module")
def encoder():
    from app.retrieval.query.medcpt import MedCPTQueryEncoder

    built = MedCPTQueryEncoder(QueryEncoderConfig(model_cache_dir=CACHE, offline=True))
    built.load()
    return built


@encoder_available
def test_loaded_encoder_reports_the_pinned_revision_and_checksums(encoder):
    config = QueryEncoderConfig()
    spec = encoder.specification
    assert spec.model_id == config.model_id
    assert spec.model_revision == config.model_revision
    assert spec.model_checksum == config.model_checksum
    assert spec.tokenizer_checksum == config.tokenizer_checksum
    assert spec.dimension == 768 and spec.pooling == "CLS" and spec.normalization == "NONE"
    assert spec.max_query_tokens == 64 and spec.distance_metric == "DOT"


@encoder_available
def test_query_vectors_are_finite_unnormalized_and_the_right_shape(encoder):
    vector = encoder.encode_queries(["What is the role of HLA-B27?"])[0]
    assert len(vector.values) == 768
    assert all(math.isfinite(value) for value in vector.values)
    # Unnormalized, as released: a normalized model would have unit norm and change every score.
    assert not math.isclose(vector.norm, 1.0, abs_tol=1e-3)


@encoder_available
def test_repeated_encoding_on_one_host_is_byte_identical(encoder):
    first = encoder.encode_queries(["ankylosing spondylitis"])[0]
    encoder._cache.clear()
    second = encoder.encode_queries(["ankylosing spondylitis"])[0]
    assert first.checksum == second.checksum


@encoder_available
def test_batched_and_single_encoding_agree_within_tolerance(encoder):
    """Numerically equivalent, not byte identical: padding changes the last bits."""
    encoder._cache.clear()
    batch = encoder.encode_queries(["sepsis management", "atrial fibrillation anticoagulation"])
    encoder._cache.clear()
    single = encoder.encode_queries(["sepsis management"])[0]
    delta = max(abs(a - b) for a, b in zip(batch[0].values, single.values, strict=True))
    assert delta < 1e-4


@encoder_available
def test_an_over_long_question_is_rejected_and_never_truncated(encoder):
    long_query = "chronic kidney disease and heart failure management considerations " * 12
    with pytest.raises(RetrievalError) as raised:
        encoder.encode_queries([long_query])
    assert raised.value.code == "QUERY_TOO_LONG"
    assert raised.value.details["limit"] == 64
    assert raised.value.details["observed"] > 64


@encoder_available
def test_measurement_uses_the_encoder_own_tokenizer(encoder):
    measurement = encoder.measure(["What is the role of HLA-B27 in spondyloarthritis?"])[0]
    assert measurement.token_count > 0 and not measurement.exceeds_limit


@encoder_available
def test_an_empty_question_is_rejected(encoder):
    with pytest.raises(RetrievalError) as raised:
        encoder.encode_queries(["   "])
    assert raised.value.code == "QUERY_EMPTY"


@encoder_available
def test_the_cache_is_scoped_to_the_encoder_version_and_returns_the_same_vector(encoder):
    encoder._cache.clear()
    first = encoder.encode_queries(["myocardial infarction"])[0]
    second = encoder.encode_queries(["myocardial infarction"])[0]
    assert second.cached and not first.cached
    assert first.checksum == second.checksum
    assert first.query_hash == second.query_hash


@encoder_available
def test_a_disabled_cache_never_serves_a_stored_vector():
    from app.retrieval.query.medcpt import MedCPTQueryEncoder

    built = MedCPTQueryEncoder(
        QueryEncoderConfig(model_cache_dir=CACHE, offline=True, cache_size=0)
    )
    first = built.encode_queries(["sepsis"])[0]
    second = built.encode_queries(["sepsis"])[0]
    assert not first.cached and not second.cached


@encoder_available
def test_a_missing_pinned_model_fails_closed_offline(tmp_path):
    from app.retrieval.query.medcpt import MedCPTQueryEncoder

    built = MedCPTQueryEncoder(QueryEncoderConfig(model_cache_dir=tmp_path, offline=True))
    with pytest.raises(RetrievalError) as raised:
        built.load()
    assert raised.value.code == "QUERY_ENCODER_UNAVAILABLE_OFFLINE"


@encoder_available
def test_a_query_vector_is_closer_to_its_own_passage_than_to_an_unrelated_one(encoder):
    """A weak sanity check on the shared vector space, reported as such and never tuned against.

    Two pairs are an anecdote, not a measurement. The measured numbers live in the retrieval
    evaluation, and nothing in the pipeline is calibrated on this assertion.
    """
    from app.embeddings.inputs import ChunkSource, build
    from app.embeddings.medcpt import MedCPTArticleEmbedder

    article = MedCPTArticleEmbedder(EmbeddingConfig(model_cache_dir=CACHE, offline=True))
    passages = {
        "match": "HLA-B27 is a class I antigen strongly associated with ankylosing spondylitis.",
        "other": "Body mass index is weight in kilograms divided by height in metres squared.",
    }
    vectors = {
        name: article.embed_documents(
            (
                build(
                    ChunkSource(
                        chunk_id=fixture_id(name),
                        chunk_type="TEXT_CHILD",
                        retrieval_text=text,
                        document_title="Synthetic Reference",
                    ),
                    EmbeddingConfig(model_cache_dir=CACHE, offline=True),
                ),
            )
        )[0].values
        for name, text in passages.items()
    }
    query = encoder.encode_queries(["Which antigen is associated with ankylosing spondylitis?"])[0]
    similarity = {
        name: sum(a * b for a, b in zip(query.values, values, strict=True))
        for name, values in vectors.items()
    }
    assert similarity["match"] > similarity["other"]


def test_query_vector_cache_does_not_retain_query_text(encoder):
    encoder._cache.clear()
    result = encoder.encode_queries(["HLA-B27 synthetic cache privacy check"])[0]
    assert result.normalized
    assert all(not value.normalized for value in encoder._cache.values())
    assert encoder.encode_queries([result.normalized])[0].normalized == result.normalized
