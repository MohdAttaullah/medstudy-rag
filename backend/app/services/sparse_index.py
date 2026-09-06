"""Durable M5 lexical indexing: analyze an already-embedded chunk set, stage it, verify, activate.

The shape mirrors M4 — a fenced lease, the transactional outbox, reconciliation before activation
— with one simplification and one strengthening.

The simplification: there is no second system. Postings live in PostgreSQL beside the expected
set, so the build commits transactionally and no cross-store reconciliation is required.

The strengthening: the corpus is not re-derived from eligibility rules. It is taken from the
*dense* lane's recorded embeddings for the same chunk dataset, so the two lanes index exactly the
same chunks by construction, and the verification step can prove it rather than assume it. A
hybrid ranking that fused candidates from two different chunk sets would be silently wrong in a
way no score could reveal.
"""

import time
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import DomainError
from app.core.retrieval_config import SparseAnalyzerConfig, SparseIndexConfig
from app.ingestion.state import transition
from app.models.chunking import Chunk, ChunkRun
from app.models.documents import Document, DocumentVersion, IngestionJob, OutboxMessage
from app.models.embeddings import ChunkEmbedding, EmbeddingRun, IndexRun
from app.models.enums import Status
from app.models.retrieval import (
    SparseDocument,
    SparseIndex,
    SparseIndexVersion,
    SparsePosting,
    SparseTerm,
    SparseValidationFinding,
)
from app.observability.ingestion import audit, phase_event
from app.retrieval.errors import MESSAGES, RetrievalError, SparseIndexCancelled
from app.retrieval.sparse.analyzer import frequencies
from app.retrieval.validation import Finding, reconcile_sparse
from app.security.auth import Principal

BUILDING = ("STAGING", "VERIFYING")


class SparseIndexService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        analyzer: SparseAnalyzerConfig,
        config: SparseIndexConfig,
        worker_identity: str = "celery-sparse-indexer",
    ) -> None:
        self.sessions = sessions
        self.analyzer = analyzer
        self.config = config
        self.worker = worker_identity

    # ------------------------------------------------------------------ helpers

    def locked(
        self, session: Session, job_id: UUID
    ) -> tuple[Document, DocumentVersion, IngestionJob]:
        job = session.get(IngestionJob, job_id)
        if job is None:
            raise RetrievalError("SPARSE_SOURCE_NOT_READY")
        version = session.get(DocumentVersion, job.document_version_id)
        if version is None:
            raise RetrievalError("SPARSE_SOURCE_NOT_READY")
        document = session.get(Document, version.document_id, with_for_update=True)
        if document is None:
            raise RetrievalError("SPARSE_SOURCE_NOT_READY")
        session.refresh(job, with_for_update=True)
        return document, version, job

    def analyzer_version(
        self, session: Session, analyzer: SparseAnalyzerConfig
    ) -> SparseIndexVersion:
        """Find or create the analyzer identity for this policy.

        Keyed on the analyzer fingerprint alone. Batch sizes and timeouts do not change what a
        term is and must not fragment the lexical vocabulary into incomparable versions.
        """
        fingerprint = analyzer.analyzer_fingerprint
        existing = session.scalar(
            select(SparseIndexVersion).where(SparseIndexVersion.analyzer_fingerprint == fingerprint)
        )
        if existing is not None:
            return existing
        version = SparseIndexVersion(
            id=uuid4(),
            analyzer_name=analyzer.analyzer_name,
            analyzer_version=analyzer.analyzer_version,
            unicode_normalization=analyzer.unicode_normalization,
            case_policy=analyzer.case_policy,
            compound_policy=analyzer.compound_policy,
            stopword_policy="DISABLED" if not analyzer.stopwords else "EXPLICIT_LIST",
            stopword_count=len(analyzer.stopwords),
            min_term_length=analyzer.min_term_length,
            max_term_length=analyzer.max_term_length,
            expansion=analyzer.expansion,
            configuration_version=analyzer.version,
            analyzer_fingerprint=fingerprint,
            policy_fingerprint=analyzer.fingerprint,
            config_snapshot=analyzer.model_dump(mode="json"),
        )
        session.add(version)
        session.flush()
        return version

    # ------------------------------------------------------------------ scheduling

    def schedule(
        self,
        job_id: UUID,
        analyzer: SparseAnalyzerConfig | None = None,
        _session: Session | None = None,
    ) -> UUID | None:
        policy = analyzer or self.analyzer
        with self.sessions.begin() if _session is None else nullcontext(_session) as session:
            document, version, job = self.locked(session, job_id)
            if document.archived_at or job.status != Status.READY_FOR_RETRIEVAL:
                return None
            existing = session.scalar(
                select(SparseIndex).where(
                    SparseIndex.ingestion_job_id == job.id,
                    SparseIndex.generation == job.retry_count,
                )
            )
            if existing:
                return existing.id
            dense = session.scalar(
                select(IndexRun)
                .where(
                    IndexRun.document_version_id == version.id,
                    IndexRun.tenant_id == version.tenant_id,
                    IndexRun.is_active.is_(True),
                    IndexRun.status == "VERIFIED",
                )
                .with_for_update()
            )
            dataset = session.scalar(
                select(ChunkRun).where(
                    ChunkRun.document_version_id == version.id,
                    ChunkRun.tenant_id == version.tenant_id,
                    ChunkRun.is_active.is_(True),
                )
            )
            # The lexical lane is built from the dense lane's chunk dataset, so a missing or
            # unverified dense index is a hard stop rather than a reason to guess a corpus.
            if dense is None or dataset is None or dense.chunk_run_id != dataset.id:
                transition(
                    session,
                    job,
                    version,
                    Status.FAILED,
                    None,
                    service=self.worker,
                    error_code="SPARSE_SOURCE_NOT_READY",
                    error_message=MESSAGES["SPARSE_SOURCE_NOT_READY"][0],
                )
                return None
            analyzer_version = self.analyzer_version(session, policy)
            index = SparseIndex(
                id=uuid4(),
                tenant_id=job.tenant_id,
                document_id=document.id,
                document_version_id=version.id,
                chunk_run_id=dataset.id,
                sparse_index_version_id=analyzer_version.id,
                ingestion_job_id=job.id,
                generation=job.retry_count,
                schema_version=self.config.schema_version,
                status="STAGING",
                policy_fingerprint=self.config.fingerprint,
                config_snapshot={
                    "analyzer": policy.model_dump(mode="json"),
                    "index": self.config.model_dump(mode="json"),
                },
                correlation_id=job.correlation_id,
            )
            session.add(index)
            session.flush()
            session.add(
                OutboxMessage(
                    job_id=job.id,
                    generation=job.retry_count,
                    kind="SPARSE_INDEX",
                    sparse_index_id=index.id,
                )
            )
            audit(
                session, job.tenant_id, None, "SPARSE_INDEX_CREATED", index.id, job.correlation_id
            )
            return index.id

    def request(
        self,
        actor: Principal,
        job_id: UUID,
        correlation_id: UUID,
        analyzer: SparseAnalyzerConfig | None = None,
    ) -> UUID | None:
        """Explicit, audited rebuild of the lexical lane alone."""
        actor.require("ingestion:reindex")
        with self.sessions.begin() as session:
            document, version, job = self.locked(session, job_id)
            if job.tenant_id != actor.tenant_id:
                raise DomainError("INGESTION_JOB_NOT_FOUND", "Ingestion job not found.", 404)
            if document.archived_at:
                raise DomainError(
                    "DOCUMENT_ARCHIVED", "Archived documents cannot be reindexed.", 409
                )
            if job.status not in {
                Status.READY_FOR_RETRIEVAL,
                Status.RETRIEVAL_READY,
                Status.NEEDS_REVIEW,
                Status.FAILED,
            }:
                raise DomainError(
                    "INGESTION_INVALID_TRANSITION",
                    "This job cannot have its lexical index rebuilt in its current state.",
                    409,
                )
            job.correlation_id = correlation_id
            if job.status != Status.READY_FOR_RETRIEVAL:
                transition(
                    session,
                    job,
                    version,
                    Status.READY_FOR_RETRIEVAL,
                    actor.user_id,
                    resparse=True,
                )
            audit(
                session,
                job.tenant_id,
                actor.user_id,
                "SPARSE_INDEX_REBUILD_REQUESTED",
                job.id,
                correlation_id,
                {"analyzer_version": (analyzer or self.analyzer).version},
            )
            return self.schedule(job_id, analyzer, _session=session)

    # ------------------------------------------------------------------ claim and fence

    def _claim(self, index_id: UUID) -> tuple[UUID, SparseAnalyzerConfig] | None:
        with self.sessions.begin() as session:
            index = session.get(SparseIndex, index_id)
            if index is None:
                return None
            document, version, job = self.locked(session, index.ingestion_job_id)
            session.refresh(index, with_for_update=True)
            if index.status != "STAGING" or index.lease_token is not None:
                return None
            if (
                document.archived_at
                or job.status != Status.READY_FOR_RETRIEVAL
                or job.retry_count != index.generation
            ):
                index.status = "CANCELLED"
                index.completed_at = datetime.now(UTC)
                return None
            analyzer = SparseAnalyzerConfig.model_validate(index.config_snapshot["analyzer"])
            token = uuid4()
            index.lease_token = token
            index.lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=self.config.lease_seconds
            )
            index.started_at = datetime.now(UTC)
            index.worker_identity = self.worker
            transition(session, job, version, Status.SPARSE_INDEXING, None, service=self.worker)
            phase_event("SPARSE_INDEX_STARTED", index.correlation_id, index_id)
            return token, analyzer

    def _fenced(
        self, session: Session, index_id: UUID, token: UUID
    ) -> tuple[SparseIndex, DocumentVersion, IngestionJob]:
        index = session.get(SparseIndex, index_id)
        if index is None:
            raise SparseIndexCancelled()
        document, version, job = self.locked(session, index.ingestion_job_id)
        session.refresh(index, with_for_update=True)
        dataset = session.get(ChunkRun, index.chunk_run_id)
        if (
            index.status not in BUILDING
            or index.lease_token != token
            or not index.lease_expires_at
            or index.lease_expires_at < datetime.now(UTC)
            or job.status not in {Status.SPARSE_INDEXING, Status.VERIFYING_SPARSE_INDEX}
            or job.retry_count != index.generation
            or document.archived_at
        ):
            raise SparseIndexCancelled()
        if dataset is None or not dataset.is_active:
            raise RetrievalError("SPARSE_SOURCE_NOT_READY")
        return index, version, job

    # ------------------------------------------------------------------ execution

    def run(self, index_id: UUID) -> None:
        claim = self._claim(index_id)
        if claim is None:
            return
        token, analyzer = claim
        started = time.perf_counter()

        def checkpoint() -> None:
            if time.perf_counter() - started > self.config.timeout_seconds:
                raise RetrievalError("SPARSE_TIMEOUT")

        try:
            self._execute(index_id, token, analyzer, checkpoint, started)
        except SparseIndexCancelled:
            self._fail(index_id, token, None, cancelled=True)
        except RetrievalError as exc:
            self._fail(index_id, token, exc.code, detail=exc.details)
        except Exception:
            self._fail(index_id, token, "SPARSE_PERSISTENCE_FAILED")

    def _execute(
        self,
        index_id: UUID,
        token: UUID,
        analyzer: SparseAnalyzerConfig,
        checkpoint: Any,
        started: float,
    ) -> None:
        with self.sessions.begin() as session:
            index, _, _ = self._fenced(session, index_id, token)
            correlation = index.correlation_id
            sources = self._sources(session, index)
            index.expected_chunk_count = len(sources)
            # Clear any residue from an earlier attempt on this same index row before writing,
            # so a retried build is a replacement rather than a merge with partial history.
            self._clear(session, index_id)

        phase_event("SPARSE_ANALYSIS_STARTED", correlation, index_id)
        documents: list[dict[str, Any]] = []
        postings: list[dict[str, Any]] = []
        document_frequency: Counter[str] = Counter()
        total_frequency: Counter[str] = Counter()
        total_length = 0
        for source in sources:
            checkpoint()
            counts, length = frequencies(source["retrieval_text"], analyzer)
            total_length += length
            documents.append(
                {
                    "sparse_index_id": index_id,
                    "tenant_id": source["tenant_id"],
                    "document_id": source["document_id"],
                    "document_version_id": source["document_version_id"],
                    "chunk_run_id": source["chunk_run_id"],
                    "chunk_id": source["chunk_id"],
                    "chunk_type": source["chunk_type"],
                    "source_type": source["source_type"],
                    "authority_level": source["authority_level"],
                    "subject": source["subject"],
                    "specialty": source["specialty"],
                    "length": length,
                    "distinct_terms": len(counts),
                }
            )
            for term, frequency in counts.items():
                postings.append(
                    {
                        "sparse_index_id": index_id,
                        "chunk_id": source["chunk_id"],
                        "term": term,
                        "term_frequency": frequency,
                    }
                )
                document_frequency[term] += 1
                total_frequency[term] += frequency

        self._persist(
            index_id,
            token,
            documents,
            postings,
            document_frequency,
            total_frequency,
            total_length,
            checkpoint,
        )
        phase_event("SPARSE_VERIFICATION_STARTED", correlation, index_id)
        self._finalize(index_id, token, int((time.perf_counter() - started) * 1000))

    def _sources(self, session: Session, index: SparseIndex) -> list[dict[str, Any]]:
        """The chunks the dense lane actually embedded, with the facets the lexical lane filters on.

        Driving the corpus from `chunk_embeddings` rather than from the eligibility rules is what
        guarantees the two lanes cover the same chunks. If the rules ever diverged, this would
        follow the dense lane rather than quietly indexing a different set.
        """
        embedding_run = session.scalar(
            select(EmbeddingRun).where(
                EmbeddingRun.document_version_id == index.document_version_id,
                EmbeddingRun.tenant_id == index.tenant_id,
                EmbeddingRun.chunk_run_id == index.chunk_run_id,
                EmbeddingRun.is_active.is_(True),
                EmbeddingRun.status == "SUCCEEDED",
            )
        )
        if embedding_run is None:
            raise RetrievalError("SPARSE_SOURCE_NOT_READY")
        document = session.get(Document, index.document_id)
        if document is None or document.archived_at:
            raise RetrievalError("SPARSE_SOURCE_NOT_READY")
        rows = session.execute(
            select(Chunk)
            .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.id)
            .where(
                ChunkEmbedding.embedding_run_id == embedding_run.id,
                ChunkEmbedding.tenant_id == index.tenant_id,
                Chunk.chunk_run_id == index.chunk_run_id,
            )
            .order_by(Chunk.sequence_number)
        ).scalars()
        sources = [
            {
                "tenant_id": index.tenant_id,
                "document_id": index.document_id,
                "document_version_id": index.document_version_id,
                "chunk_run_id": index.chunk_run_id,
                "chunk_id": chunk.id,
                "chunk_type": chunk.chunk_type,
                "source_type": document.source_type.value,
                "authority_level": document.authority_level.value,
                "subject": document.subject,
                "specialty": document.specialty,
                "retrieval_text": chunk.retrieval_text,
            }
            for chunk in rows
        ]
        if len(sources) > self.config.max_chunks:
            raise RetrievalError("SPARSE_RESOURCE_LIMIT", {"chunks": len(sources)})
        return sources

    def _clear(self, session: Session, index_id: UUID) -> None:
        for table in (SparsePosting, SparseTerm, SparseDocument):
            session.execute(delete(table).where(table.sparse_index_id == index_id))

    def _persist(
        self,
        index_id: UUID,
        token: UUID,
        documents: list[dict[str, Any]],
        postings: list[dict[str, Any]],
        document_frequency: Counter[str],
        total_frequency: Counter[str],
        total_length: int,
        checkpoint: Any,
    ) -> None:
        size = self.config.posting_batch_size
        with self.sessions.begin() as session:
            index, version, job = self._fenced(session, index_id, token)
            now = datetime.now(UTC)
            for start in range(0, len(documents), size):
                checkpoint()
                session.execute(
                    insert(SparseDocument),
                    [
                        {"id": uuid4(), "created_at": now, "updated_at": now, **row}
                        for row in documents[start : start + size]
                    ],
                )
            for start in range(0, len(postings), size):
                checkpoint()
                session.execute(insert(SparsePosting), postings[start : start + size])
            terms = [
                {
                    "sparse_index_id": index_id,
                    "term": term,
                    "document_frequency": frequency,
                    "total_frequency": total_frequency[term],
                }
                for term, frequency in sorted(document_frequency.items())
            ]
            for start in range(0, len(terms), size):
                checkpoint()
                session.execute(insert(SparseTerm), terms[start : start + size])
            index.indexed_chunk_count = len(documents)
            index.posting_count = len(postings)
            index.term_count = len(terms)
            index.total_length = total_length
            index.status = "VERIFYING"
            transition(
                session, job, version, Status.VERIFYING_SPARSE_INDEX, None, service=self.worker
            )

    def _finalize(self, index_id: UUID, token: UUID, duration: int) -> None:
        with self.sessions.begin() as session:
            index, version, job = self._fenced(session, index_id, token)
            outcome = reconcile_sparse(session, index, self.config)
            self._record(session, index_id, list(outcome.findings))
            index.verified_chunk_count = outcome.verified
            index.corpus_fingerprint = outcome.corpus_fingerprint
            index.completed_at = datetime.now(UTC)
            index.duration_ms = duration
            index.lease_expires_at = None
            index.metrics = {
                "expected_chunks": outcome.expected,
                "indexed_chunks": index.indexed_chunk_count,
                "verified_chunks": outcome.verified,
                "postings": index.posting_count,
                "terms": index.term_count,
                "total_length": index.total_length,
                "average_length": (
                    index.total_length / index.indexed_chunk_count
                    if index.indexed_chunk_count
                    else 0.0
                ),
                "findings": len(outcome.findings),
            }
            if not outcome.ok:
                # A failed rebuild deliberately leaves the previous active lexical index alone:
                # the corpus that was already verified must not be removed by a bad replacement.
                index.status = "FAILED"
                index.is_active = False
                index.error_code = "SPARSE_RECONCILIATION_FAILED"
                index.error_message = MESSAGES["SPARSE_RECONCILIATION_FAILED"][0]
                transition(
                    session,
                    job,
                    version,
                    Status.FAILED,
                    None,
                    service=self.worker,
                    error_code=index.error_code,
                    error_message=index.error_message,
                )
                audit(
                    session,
                    index.tenant_id,
                    None,
                    "SPARSE_INDEX_FAILED",
                    index_id,
                    index.correlation_id,
                    {"findings": len(outcome.findings)},
                )
                return

            session.execute(
                update(SparseIndex)
                .where(
                    SparseIndex.document_version_id == index.document_version_id,
                    SparseIndex.is_active.is_(True),
                    SparseIndex.id != index_id,
                )
                .values(is_active=False, status="SUPERSEDED")
            )
            # Written together so the row moves from VERIFYING straight to active and verified.
            index.status = "VERIFIED"
            index.is_active = True
            index.activated_at = datetime.now(UTC)
            transition(session, job, version, Status.RETRIEVAL_READY, None, service=self.worker)
            audit(
                session,
                index.tenant_id,
                None,
                "SPARSE_INDEX_ACTIVATED",
                index_id,
                index.correlation_id,
                {"chunks": outcome.verified, "terms": index.term_count},
            )
            phase_event("SPARSE_INDEX_ACTIVATED", index.correlation_id, index_id)

    # ------------------------------------------------------------------ findings and failure

    def _record(self, session: Session, index_id: UUID, findings: list[Finding]) -> None:
        for finding in findings:
            session.add(
                SparseValidationFinding(
                    sparse_index_id=index_id,
                    chunk_id=finding.chunk_id,
                    severity=finding.severity,
                    code=finding.code,
                    message=finding.message,
                    details=finding.details,
                )
            )

    def _fail(
        self,
        index_id: UUID,
        token: UUID,
        code: str | None,
        cancelled: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.sessions.begin() as session:
            index = session.get(SparseIndex, index_id)
            if index is None:
                return
            _, version, job = self.locked(session, index.ingestion_job_id)
            session.refresh(index, with_for_update=True)
            if index.status not in BUILDING or index.lease_token != token:
                return
            index.status = "CANCELLED" if cancelled else "FAILED"
            index.is_active = False
            index.completed_at = datetime.now(UTC)
            index.lease_expires_at = None
            index.error_code = code
            index.error_message = MESSAGES[code][0] if code else None
            if detail:
                session.add(
                    SparseValidationFinding(
                        sparse_index_id=index_id,
                        chunk_id=None,
                        severity="CRITICAL",
                        code=code or "SPARSE_BUILD_FAILED",
                        message=index.error_message or MESSAGES["SPARSE_BUILD_FAILED"][0],
                        details={key: value for key, value in detail.items() if key != "retryable"},
                    )
                )
            if (
                job.status in {Status.SPARSE_INDEXING, Status.VERIFYING_SPARSE_INDEX}
                and job.retry_count == index.generation
            ):
                transition(
                    session,
                    job,
                    version,
                    Status.CANCELLED if cancelled else Status.FAILED,
                    None,
                    service=self.worker,
                    error_code=index.error_code,
                    error_message=index.error_message,
                )
            audit(
                session,
                index.tenant_id,
                None,
                "SPARSE_INDEX_CANCELLED" if cancelled else "SPARSE_INDEX_FAILED",
                index_id,
                index.correlation_id,
                {"code": code},
            )

    # ------------------------------------------------------------------ sweeping

    def sweep(self) -> int:
        with self.sessions() as session:
            jobs = list(
                session.scalars(
                    select(IngestionJob.id)
                    .where(IngestionJob.status == Status.READY_FOR_RETRIEVAL)
                    .limit(20)
                )
            )
            expired = list(
                session.execute(
                    select(SparseIndex.id, SparseIndex.lease_token).where(
                        SparseIndex.status.in_(BUILDING),
                        SparseIndex.lease_expires_at < datetime.now(UTC),
                    )
                )
            )
        for job_id in jobs:
            self.schedule(job_id)
        for index_id, token in expired:
            self._fail(index_id, token, "SPARSE_LEASE_EXPIRED")
        with self.sessions.begin() as session:
            pending = session.scalars(
                select(OutboxMessage)
                .join(SparseIndex, SparseIndex.id == OutboxMessage.sparse_index_id)
                .where(
                    SparseIndex.status == "STAGING",
                    SparseIndex.lease_token.is_(None),
                    OutboxMessage.received_at
                    < datetime.now(UTC) - timedelta(seconds=self.config.receipt_recovery_seconds),
                )
                .with_for_update(of=OutboxMessage, skip_locked=True)
                .limit(20)
            )
            for message in pending:
                message.received_at = None
        return len(jobs) + len(expired)


def active_sparse_statistics(session: Session, index_ids: tuple[UUID, ...]) -> tuple[int, int]:
    """Document count and total length across a set of lexical indexes."""
    if not index_ids:
        return 0, 0
    count, length = session.execute(
        select(
            func.coalesce(func.sum(SparseIndex.indexed_chunk_count), 0),
            func.coalesce(func.sum(SparseIndex.total_length), 0),
        ).where(SparseIndex.id.in_(index_ids))
    ).one()
    return int(count), int(length)
