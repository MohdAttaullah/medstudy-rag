"""M6 orchestration, preserving M5 diagnostics and corpus consistency."""

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID

from app.core.chunking_config import ChunkingConfig
from app.core.config import Settings
from app.core.errors import DomainError
from app.evidence.assembly import EvidenceAssembler
from app.evidence.model import EvidenceSet, EvidenceWarning
from app.ingestion.chunking.tokenizer import LocalTokenizer
from app.repositories.evidence import EvidenceRepository
from app.repositories.retrieval import resolve_corpus
from app.reranking.model import Reranker, RerankingError, RerankInput, input_hash, ordered
from app.reranking.remote import build_reranker, verify_spec
from app.retrieval.model import RetrievalFilters
from app.retrieval.query.normalize import normalize
from app.security.auth import Principal
from app.services.progress import reporter
from app.services.retrieval import RetrievalService


class EvidenceService:
    def __init__(
        self, retrieval: RetrievalService, settings: Settings, reranker: Reranker | None = None
    ) -> None:
        self.retrieval, self.settings = retrieval, settings
        self._reranker = reranker
        self.tokens = LocalTokenizer(ChunkingConfig())

    def search(
        self,
        actor: Principal,
        query: str,
        correlation_id: UUID,
        filters: RetrievalFilters | None = None,
    ) -> dict[str, Any]:
        try:
            return self._search(actor, query, correlation_id, filters)
        except DomainError as exc:
            if self.retrieval.metrics:
                self.retrieval.metrics.failures.labels(mode="RERANKED", code=exc.code).inc()
            logging.getLogger("medical_rag.retrieval").info(
                "evidence_failed",
                extra={
                    "event": "evidence_failed",
                    "request_id": str(correlation_id),
                    "code": exc.code,
                },
            )
            raise

    def _search(
        self,
        actor: Principal,
        query: str,
        correlation_id: UUID,
        filters: RetrievalFilters | None = None,
    ) -> dict[str, Any]:
        actor.require_any("retrieval:search", "ask:submit")
        started = perf_counter()
        r = self.retrieval
        c = self.settings
        # Separate query-side cap; lane sizes/analyzer/BM25/RRF remain the frozen M5 policy.
        config = r.config.model_copy(
            update={"version": "retrieval-m6-pool-v1", "final_top_k": c.reranking.candidate_top_k}
        )
        first = RetrievalService(
            r.sessions,
            r.encoder_config,
            r.analyzer,
            config,
            encoder_factory=r.encoder,
            index_factory=r.index,
            metrics=r.metrics,
        )
        progress = reporter()
        progress.start("RETRIEVAL")
        candidates = first.search(
            actor,
            query,
            correlation_id,
            mode="HYBRID_RRF",
            top_k=c.reranking.candidate_top_k,
            filters=filters,
        )
        hydration_started = perf_counter()
        with r.sessions() as session:
            corpus = resolve_corpus(session, actor.tenant_id)
            if (
                corpus.index_run_ids != candidates.trace.index_run_ids
                or corpus.sparse_index_ids != candidates.trace.sparse_index_ids
                or corpus.chunk_run_ids != candidates.trace.chunk_run_ids
            ):
                raise RerankingError("CONTEXT_SOURCE_LINEAGE_MISMATCH")
            repository = EvidenceRepository(session, actor.tenant_id, corpus)
            sources = {
                hit.chunk_id: repository.source(hit.chunk_id) for hit in candidates.candidates
            }
        hydration_ms = (perf_counter() - hydration_started) * 1000
        # Retrieval is done once its candidates are hydrated; what follows reads them.
        progress.complete("RETRIEVAL")
        normalized = normalize(query, c.query_encoder)
        inputs = [
            RerankInput(
                chunk_id=h.chunk_id,
                text=sources[h.chunk_id].retrieval_text,
                fused_rank=h.fused_rank,
            )
            for h in candidates.candidates
        ]
        rerank_started = perf_counter()
        progress.start("RERANK")
        if self._reranker is None:
            self._reranker = build_reranker(c.reranker)
        spec = self._reranker.specification
        verify_spec(spec, c.reranker)
        results = ordered(inputs, self._reranker.rerank(normalized, inputs))
        if any(
            result.input_hash != input_hash(normalized, sources[result.chunk_id].retrieval_text)
            or not 0 < result.token_count <= 512
            for result in results
        ):
            raise RerankingError("RERANKER_INFERENCE_FAILED")
        rerank_ms = (perf_counter() - rerank_started) * 1000
        progress.complete("RERANK")
        anchors = [sources[result.chunk_id] for result in results[: c.reranking.final_top_k]]
        # EVIDENCE spans expansion and assembly here and closes at the sufficiency gate, which is
        # the decision the stage describes and which M7 owns.
        progress.start("EVIDENCE")
        expansion_started = perf_counter()
        with r.sessions() as session:
            if resolve_corpus(session, actor.tenant_id) != corpus:
                raise RerankingError("CONTEXT_SOURCE_LINEAGE_MISMATCH")
            repository = EvidenceRepository(session, actor.tenant_id, corpus)
            relatives = {a.chunk_id: repository.relatives(a) for a in anchors}
            if resolve_corpus(session, actor.tenant_id) != corpus:
                raise RerankingError("CONTEXT_SOURCE_LINEAGE_MISMATCH")
        expansion_ms = (perf_counter() - expansion_started) * 1000
        assembly_started = perf_counter()
        blocks, warnings, duplicates = EvidenceAssembler(
            c.expansion, c.evidence_budget, self.tokens.count
        ).assemble(anchors, relatives)
        assembly_ms = (perf_counter() - assembly_started) * 1000
        durations = {
            **candidates.trace.durations_ms,
            "evidence_hydration_ms": hydration_ms,
            "reranking_ms": rerank_ms,
            "expansion_ms": expansion_ms,
            "assembly_ms": assembly_ms,
            "pipeline_total_ms": (perf_counter() - started) * 1000,
        }
        if r.metrics:
            for stage, value in (
                ("reranking", rerank_ms),
                ("expansion", expansion_ms),
                ("evidence_hydration", hydration_ms),
                ("assembly", assembly_ms),
            ):
                r.metrics.evidence_stage.labels(stage=stage).observe(value / 1000)
        # An anchor whose required context could not be satisfied is unusable evidence, and
        # unusable evidence is removed rather than flagged: generation, citation binding and
        # verification all read `evidence_blocks`, so leaving it there with a warning would put
        # an incomplete fragment in the prompt and in the citable set. Its own expansions go with
        # it — they are context for evidence that no longer exists. See ADR-020.
        unusable = {
            w.anchor_chunk_id
            for w in warnings
            if w.code == "CONTEXT_REQUIRED_PARENT_MISSING" and w.anchor_chunk_id
        }
        usable = [b for b in blocks if b.anchor_chunk_id not in unusable]
        excluded = [b for b in blocks if b.anchor_chunk_id in unusable]
        selected = {b.anchor_chunk_id for b in usable}
        trace = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tenant_id": str(actor.tenant_id),
            "correlation_id": str(correlation_id),
            "reranker": spec.model_dump(mode="json"),
            "reranking_config": c.reranking.model_dump(),
            "reranking_fingerprint": c.reranking.fingerprint,
            "expansion_config": c.expansion.model_dump(),
            "expansion_fingerprint": c.expansion.fingerprint,
            "budget_config": c.evidence_budget.model_dump(),
            "budget_fingerprint": c.evidence_budget.fingerprint,
            "retrieval_config": config.model_dump(),
            "durations_ms": durations,
            "score_semantics": "RAW_LOGIT_RANKING_DIAGNOSTIC",
        }
        evidence = EvidenceSet(
            query_hash=candidates.trace.query_hash,
            retrieval_trace=asdict(candidates.trace),
            reranking_trace=trace,
            anchors=[a.chunk_id for a in anchors if a.chunk_id in selected],
            expansions=[b.evidence_id for b in usable if b.expansion_reason != "RERANKED_ANCHOR"],
            evidence_blocks=usable,
            total_tokens=sum(b.token_count for b in usable),
            requires_visual_evidence=any(b.requires_visual_evidence for b in usable),
            excluded_anchors=sorted(unusable, key=str),
            excluded_blocks=excluded,
            # M5's own warnings concern the query rather than any one candidate, so they
            # carry the RETRIEVAL tier and name no chunk.
            warnings=[*candidates.warnings, *(w.render() for w in warnings)],
            warning_details=[
                *(EvidenceWarning(code=code) for code in candidates.warnings),
                *warnings,
            ],
            duplicates_removed=duplicates,
        )
        hits = {hit.chunk_id: hit for hit in candidates.candidates}
        return {
            "correlation_id": correlation_id,
            "mode": "RERANKED",
            "answering_enabled": False,
            "first_stage": asdict(candidates),
            "reranked": [
                {
                    **asdict(hits[result.chunk_id]),
                    **result.model_dump(),
                    "selected_anchor": result.chunk_id in selected,
                }
                for result in results
            ],
            "evidence_set": evidence.model_dump(mode="json"),
        }
