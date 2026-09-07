"""M6 safety and representation contracts, with actual pinned model checks."""

from dataclasses import replace
from uuid import uuid4

import pytest
from app.core.chunking_config import ChunkingConfig
from app.core.reranking_config import (
    MODEL_FILES,
    EvidenceBudgetConfig,
    ExpansionConfig,
    RerankerConfig,
    RerankingConfig,
)
from app.evidence.assembly import EvidenceAssembler
from app.evidence.model import ArtifactRef, EvidenceSource, SourceSpan
from app.ingestion.chunking.tokenizer import LocalTokenizer
from app.reranking.medcpt import MedCPTReranker
from app.reranking.model import RerankingError, RerankInput, RerankResult, input_hash, ordered
from app.reranking.remote import verify_spec
from app.retrieval.model import Provenance
from pydantic import ValidationError


def source(
    text="A source paragraph",
    *,
    base=None,
    kind="TEXT_CHILD",
    start=0,
    element=None,
    parent=None,
    sequence=1,
):
    tenant = base.tenant_id if base else uuid4()
    p = (
        replace(base.provenance, chunk_type=kind, parent_chunk_id=parent, sequence_number=sequence)
        if base
        else Provenance(
            document_id=uuid4(),
            document_version_id=uuid4(),
            chunk_run_id=uuid4(),
            document_title="Synthetic source",
            source_type="TEXTBOOK",
            authority_level="REFERENCE",
            subject=None,
            specialty=None,
            chunk_type=kind,
            page_start=1,
            page_end=1,
            sequence_number=sequence,
            parent_chunk_id=parent,
            question_id=None,
        )
    )
    span = SourceSpan(
        element_id=element or uuid4(),
        start=start,
        end=start + len(text),
        text=text,
        page=1,
        reading_order=sequence,
        role="SOURCE",
        bbox=(0, 0, 10, 10),
    )
    return EvidenceSource(
        tenant_id=tenant,
        chunk_id=uuid4(),
        parse_run_id=base.parse_run_id if base else uuid4(),
        provenance=p,
        retrieval_text=text,
        text=text,
        spans=[span],
    )


def assemble(anchors, relatives=None, **budget):
    return EvidenceAssembler(
        ExpansionConfig(previous_siblings=1, next_siblings=1),
        EvidenceBudgetConfig(**budget),
        LocalTokenizer(ChunkingConfig()).count,
    ).assemble(anchors, relatives or {})


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "other"),
        ("model_revision", "main"),
        ("tokenizer_revision", "main"),
        ("dtype", "float16"),
        ("offline", False),
        ("score_semantics", "PROBABILITY"),
        ("truncation_policy", "TRUNCATE"),
    ],
)
def test_model_semantics_are_pinned(field, value):
    with pytest.raises(ValidationError):
        RerankerConfig(**{field: value})


def test_invalid_anchor_budget():
    with pytest.raises(ValidationError):
        RerankingConfig(candidate_top_k=2, final_top_k=3)


def test_deterministic_ties_and_full_input_hash():
    a, b = [RerankInput(chunk_id=uuid4(), text="Passage " * 100, fused_rank=i) for i in (8, 1)]
    results = [
        RerankResult(
            chunk_id=c.chunk_id,
            reranker_score=1,
            input_hash=input_hash("query", c.text),
            token_count=203,
        )
        for c in (a, b)
    ]
    assert ordered([a, b], results)[0].chunk_id == b.chunk_id
    assert input_hash("query", a.text) != input_hash("query", a.text[:320])
    assert input_hash("ab", "c") != input_hash("a", "bc")


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_fails_closed(score):
    item = RerankInput(chunk_id=uuid4(), text="text", fused_rank=1)
    with pytest.raises(RerankingError, match="RERANKER_NONFINITE_SCORE"):
        ordered(
            [item],
            [
                RerankResult(
                    chunk_id=item.chunk_id, reranker_score=score, input_hash="x", token_count=1
                )
            ],
        )


def test_missing_or_duplicate_model_outputs_fail_closed():
    item = RerankInput(chunk_id=uuid4(), text="text", fused_rank=1)
    with pytest.raises(RerankingError, match="INFERENCE_FAILED"):
        ordered([item], [])


def test_missing_cache(tmp_path):
    with pytest.raises(RerankingError, match="RERANKER_UNAVAILABLE"):
        MedCPTReranker(RerankerConfig(model_cache_dir=tmp_path)).load()


def test_corrupted_cache_rejected_before_deserialization(tmp_path):
    from unittest.mock import patch

    for name in MODEL_FILES:
        (tmp_path / name).write_text("corrupt")
    with (
        patch("huggingface_hub.snapshot_download", return_value=str(tmp_path)),
        pytest.raises(RerankingError, match="MODEL_MISMATCH"),
    ):
        MedCPTReranker(RerankerConfig(model_cache_dir=tmp_path)).load()


@pytest.fixture(scope="module")
def reranker():
    return MedCPTReranker(RerankerConfig())


def test_real_logits_stability_batch_and_rank_eight_promotion(reranker):
    query = "What is the role of insulin?"
    items = [
        RerankInput(chunk_id=uuid4(), text="The femur is a long bone.", fused_rank=i)
        for i in range(1, 8)
    ]
    items.append(RerankInput(chunk_id=uuid4(), text="Insulin lowers blood glucose.", fused_rank=8))
    first = reranker.rerank(query, items)
    assert first[0].chunk_id == items[-1].chunk_id
    assert first == reranker.rerank(query, items)
    single = reranker.rerank(query, [items[-1]])[0]
    assert abs(first[0].reranker_score - single.reranker_score) < 1e-4
    assert first[-1].reranker_score < 0  # Raw logits, not sigmoid output.
    verify_spec(reranker.specification, RerankerConfig())
    with pytest.raises(RerankingError, match="MODEL_MISMATCH"):
        verify_spec(
            reranker.specification.model_copy(update={"model_revision": "other"}), RerankerConfig()
        )


def test_real_overflow_rejects_without_truncation(reranker):
    with pytest.raises(RerankingError, match="INPUT_TOO_LONG"):
        reranker.rerank(
            "query", [RerankInput(chunk_id=uuid4(), text="insulin " * 600, fused_rank=1)]
        )


def test_duplicate_chunks_and_same_source_removed_distinct_documents_preserved():
    a = source()
    same = a.model_copy(update={"chunk_id": uuid4()})
    other = source(a.text)
    blocks, warnings, duplicates = assemble([a, a, same, other])
    assert len(blocks) == 2 and duplicates == 2 and not warnings
    assert len({b.evidence_id for b in blocks}) == 2


def test_overlap_removed_by_offsets_preserves_new_source_text():
    a = source("ABCDE", element=uuid4())
    b = source("DEFGH", base=a, start=3, element=a.spans[0].element_id)
    blocks, _, _ = assemble([a, b])
    assert [b.text for b in blocks] == ["ABCDE", "FGH"]
    assert blocks[1].source_spans[0].start == 5


def test_parent_context_is_conditional_and_anchor_reserved():
    parent = source("The first clause and the second clause", kind="TEXT_PARENT")
    child = source(
        "The first clause", base=parent, element=parent.spans[0].element_id, parent=parent.chunk_id
    )
    blocks, _, _ = assemble([child], {child.chunk_id: [parent]})
    assert [b.expansion_reason for b in blocks] == ["RERANKED_ANCHOR", "PARENT_EXPANSION"]
    assert blocks[1].text == " and the second clause"
    tight, findings, _ = assemble([child], {child.chunk_id: [parent]}, max_total_tokens=4)
    assert len(tight) == 1 and findings


@pytest.mark.parametrize("bad", ["tenant", "run", "parent"])
def test_invalid_expansion_fails_closed(bad):
    a = source(parent=uuid4())
    b = source("next", base=a, parent=a.provenance.parent_chunk_id, sequence=2)
    if bad == "tenant":
        b = b.model_copy(update={"tenant_id": uuid4()})
    if bad == "run":
        b = b.model_copy(update={"provenance": replace(b.provenance, chunk_run_id=uuid4())})
    if bad == "parent":
        b = b.model_copy(update={"provenance": replace(b.provenance, parent_chunk_id=uuid4())})
    with pytest.raises(RerankingError):
        assemble([a], {a.chunk_id: [b]})


def test_neighbours_and_source_order():
    a = source("anchor.", parent=uuid4(), sequence=2)
    before = source("before.", base=a, parent=a.provenance.parent_chunk_id, sequence=1)
    after = source("after.", base=a, parent=a.provenance.parent_chunk_id, sequence=3)
    blocks, _, _ = assemble([a], {a.chunk_id: [after, before]})
    assert [b.expansion_reason for b in blocks] == [
        "RERANKED_ANCHOR",
        "PREVIOUS_SIBLING",
        "NEXT_SIBLING",
    ]
    zero = EvidenceAssembler(
        ExpansionConfig(parent_enabled=False, previous_siblings=0, next_siblings=0),
        EvidenceBudgetConfig(),
        len,
    ).assemble([a], {a.chunk_id: [before, after]})
    assert len(zero[0]) == 1


@pytest.mark.parametrize("kind", ["TABLE_PART", "FORMULA", "FIGURE_CONTEXT", "QUESTION"])
def test_atomic_source_preserved_and_never_budget_truncated(kind):
    a = source("Source exact expression and explicit options", kind=kind)
    if kind != "QUESTION":
        a = a.model_copy(
            update={
                "artifacts": [
                    ArtifactRef(
                        artifact_id=uuid4(),
                        kind="FIGURE" if kind == "FIGURE_CONTEXT" else kind,
                        source_element_id=a.spans[0].element_id,
                        href="/authorized",
                    )
                ]
            }
        )
    else:
        a = a.model_copy(
            update={"question": {"explicit_answer": None, "assessment_material": True}}
        )
    blocks, _, _ = assemble([a])
    assert blocks[0].text == a.text
    assert blocks[0].requires_visual_evidence == (kind == "FIGURE_CONTEXT")
    assert not assemble([a], max_tokens_per_block=1)[0]


def test_missing_source_provenance_rejected():
    with pytest.raises(RerankingError, match="PROVENANCE_MISSING"):
        assemble([source().model_copy(update={"spans": []})])


@pytest.mark.parametrize("kind", ["timeout", "unavailable", "mismatch", "inference"])
def test_remote_failures_do_not_fallback(monkeypatch, kind):
    import httpx
    from app.reranking.remote import HttpReranker

    def post(*args, **kwargs):
        if kind == "timeout":
            raise httpx.ReadTimeout("private query omitted")
        code = {
            "unavailable": "RERANKER_UNAVAILABLE",
            "mismatch": "RERANKER_MODEL_MISMATCH",
            "inference": "RERANKER_INFERENCE_FAILED",
        }[kind]
        return httpx.Response(503, json={"error": {"code": code}})

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(RerankingError, match="RERANKER_") as error:
        HttpReranker(RerankerConfig(endpoint="http://internal")).rerank("private query", [])
    assert "private" not in error.value.message


def test_regression_evaluation_detects_demoted_relevant_candidate():
    from app.evaluation.reranking import failures

    ids = [uuid4() for _ in range(8)]
    assert "RERANKER_REGRESSION" in failures({ids[0]}, ids, ids, ids[1:] + ids[:1])
    assert "FIRST_STAGE_MISS" in failures({uuid4()}, ids, ids, ids)
    assert failures(set(), ids, ids, ids) == ["CORPUS_LACKS_EVIDENCE"]


def test_visual_only_original_remains_inspectable():
    a = source("", kind="FIGURE_CONTEXT")
    a = a.model_copy(
        update={
            "retrieval_text": "Source figure; no source caption or explanatory text.",
            "artifacts": [
                ArtifactRef(
                    artifact_id=uuid4(),
                    kind="FIGURE",
                    source_element_id=a.spans[0].element_id,
                    href="/authorized",
                    image_available=True,
                )
            ],
        }
    )
    blocks, _, _ = assemble([a])
    assert len(blocks) == 1 and blocks[0].requires_visual_evidence
    assert blocks[0].source_element_ids == [a.spans[0].element_id]
    assert "FIGURE_CAPTION" not in blocks[0].context_reasons
    duplicate = a.model_copy(update={"chunk_id": uuid4()})
    assert len(assemble([a, duplicate])[0]) == 1


def test_atomic_question_retains_all_contributing_chunk_ids():
    a = source("Question and options", kind="QUESTION")
    ids = [a.chunk_id, uuid4()]
    a = a.model_copy(
        update={
            "source_chunk_ids": ids,
            "question": {"explicit_answer": None},
            "evidence_text": "Question and options\nSource explanation",
        }
    )
    blocks, _, _ = assemble([a])
    assert blocks[0].source_chunk_ids == ids
    assert blocks[0].question["explicit_answer"] is None


def test_partial_pool_miss_is_identified():
    from app.evaluation.reranking import failures

    found, missing = uuid4(), uuid4()
    assert "FIRST_STAGE_MISS" in failures({found, missing}, [found], [found], [found])
