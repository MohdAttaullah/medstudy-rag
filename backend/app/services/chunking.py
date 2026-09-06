"""Durable M3 orchestration, using the existing outbox and a fenced chunk-run lease."""

import time
from collections import defaultdict
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.chunking_config import ChunkingConfig
from app.core.errors import DomainError
from app.ingestion.chunking.builder import Builder, input_fingerprint
from app.ingestion.chunking.errors import MESSAGES, ChunkCancelled, ChunkError
from app.ingestion.chunking.model import ChunkDataset, ChunkInput, Finding, Span
from app.ingestion.chunking.quality import validate
from app.ingestion.chunking.tokenizer import LocalTokenizer
from app.ingestion.state import transition
from app.models.chunking import (
    Chunk,
    ChunkArtifactRelation,
    ChunkRelation,
    ChunkRun,
    ChunkSourceElement,
    ChunkSourcePage,
    ChunkValidationFinding,
    QuestionArtifact,
    QuestionOption,
)
from app.models.documents import Document, DocumentVersion, IngestionJob, OutboxMessage
from app.models.enums import Status
from app.models.parsing import ParseRun
from app.observability.ingestion import audit, phase_event
from app.repositories.chunking import load_source
from app.security.auth import Principal


class ChunkService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        config: ChunkingConfig,
        worker_identity: str = "celery-chunker",
    ) -> None:
        self.sessions, self.config, self.worker = sessions, config, worker_identity

    def locked(
        self, session: Session, job_id: UUID
    ) -> tuple[Document, DocumentVersion, IngestionJob]:
        initial = session.get(IngestionJob, job_id)
        if initial is None:
            raise ChunkError("CHUNK_SOURCE_PARSE_NOT_READY")
        version = session.get(DocumentVersion, initial.document_version_id)
        if version is None:
            raise ChunkError("CHUNK_SOURCE_PARSE_NOT_READY")
        document = session.get(Document, version.document_id, with_for_update=True)
        if document is None:
            raise ChunkError("CHUNK_SOURCE_PARSE_NOT_READY")
        session.refresh(initial, with_for_update=True)
        return document, version, initial

    def schedule(
        self,
        job_id: UUID,
        force: bool = False,
        policy: ChunkingConfig | None = None,
        _session: Session | None = None,
    ) -> UUID | None:
        config = policy or self.config
        with self.sessions.begin() if _session is None else nullcontext(_session) as session:
            document, version, job = self.locked(session, job_id)
            if document.archived_at or job.status != Status.READY_FOR_CHUNKING:
                return None
            existing = session.scalar(
                select(ChunkRun).where(
                    ChunkRun.ingestion_job_id == job.id, ChunkRun.generation == job.retry_count
                )
            )
            if existing:
                return existing.id
            parsed = session.scalar(
                select(ParseRun)
                .where(
                    ParseRun.document_version_id == version.id,
                    ParseRun.tenant_id == version.tenant_id,
                    ParseRun.is_active.is_(True),
                )
                .with_for_update()
            )
            if (
                parsed is None
                or parsed.status != "SUCCEEDED"
                or parsed.validation_result not in {"PASS", "PASS_WITH_WARNINGS"}
            ):
                transition(
                    session,
                    job,
                    version,
                    Status.FAILED,
                    None,
                    service=self.worker,
                    error_code="CHUNK_SOURCE_PARSE_NOT_READY",
                    error_message=MESSAGES["CHUNK_SOURCE_PARSE_NOT_READY"][0],
                )
                return None
            previous = session.scalar(
                select(ChunkRun).where(
                    ChunkRun.document_version_id == version.id,
                    ChunkRun.parse_run_id == parsed.id,
                    ChunkRun.policy_fingerprint == config.fingerprint,
                    ChunkRun.is_active.is_(True),
                )
            )
            if previous and not force and self._reusable(session, previous, config):
                for state in (
                    Status.CHUNKING,
                    Status.VALIDATING_CHUNKS,
                    Status.READY_FOR_EMBEDDING,
                ):
                    transition(session, job, version, state, None, service=self.worker)
                audit(
                    session,
                    job.tenant_id,
                    None,
                    "CHUNK_RUN_REUSED",
                    previous.id,
                    job.correlation_id,
                )
                return previous.id
            run = ChunkRun(
                id=uuid4(),
                tenant_id=job.tenant_id,
                document_id=document.id,
                document_version_id=version.id,
                parse_run_id=parsed.id,
                ingestion_job_id=job.id,
                generation=job.retry_count,
                chunker_name=config.chunker_name,
                chunker_version=config.chunker_version,
                configuration_version=config.version,
                policy_fingerprint=config.fingerprint,
                config_snapshot=config.model_dump(mode="json"),
                tokenizer_name=config.tokenizer_name,
                tokenizer_version=config.tokenizer_revision + "/" + config.tokenizer_runtime,
                status="PENDING",
                force=force,
                correlation_id=job.correlation_id,
            )
            session.add(run)
            session.flush()
            session.add(
                OutboxMessage(
                    job_id=job.id, generation=job.retry_count, kind="CHUNKING", chunk_run_id=run.id
                )
            )
            audit(session, job.tenant_id, None, "CHUNK_RUN_CREATED", run.id, job.correlation_id)
            return run.id

    def _reusable(self, session: Session, previous: ChunkRun, config: ChunkingConfig) -> bool:
        """Is a completed dataset genuinely the answer for the current input?

        The policy fingerprint covers the chunker, its targets, thresholds and the pinned
        tokenizer, and the query already fixed the active parse run. That is still an identity
        argument rather than a content one, so the normalized source is re-fingerprinted and
        compared. Anything that cannot be proved identical is rebuilt instead of reused.
        """
        if not previous.input_fingerprint or previous.policy_fingerprint != config.fingerprint:
            return False
        try:
            source = load_source(session, previous, config)
        except (ChunkError, DomainError):
            return False
        return input_fingerprint(source) == previous.input_fingerprint

    def request(
        self,
        actor: Principal,
        job_id: UUID,
        correlation_id: UUID,
        force: bool = False,
        config: ChunkingConfig | None = None,
    ) -> UUID | None:
        actor.require("ingestion:rechunk")
        with self.sessions.begin() as session:
            document, version, job = self.locked(session, job_id)
            if job.tenant_id != actor.tenant_id:
                raise DomainError("INGESTION_JOB_NOT_FOUND", "Ingestion job not found.", 404)
            if document.archived_at:
                raise DomainError(
                    "DOCUMENT_ARCHIVED", "Archived documents cannot be rechunked.", 409
                )
            if job.status not in {
                Status.READY_FOR_CHUNKING,
                Status.READY_FOR_EMBEDDING,
                Status.NEEDS_REVIEW,
                Status.FAILED,
            }:
                raise DomainError(
                    "INGESTION_INVALID_TRANSITION",
                    "This job cannot be rechunked in its current state.",
                    409,
                )
            job.correlation_id = correlation_id
            if job.status != Status.READY_FOR_CHUNKING:
                transition(
                    session, job, version, Status.READY_FOR_CHUNKING, actor.user_id, rechunk=True
                )
            audit(
                session,
                job.tenant_id,
                actor.user_id,
                "CHUNK_REPROCESS_REQUESTED",
                job.id,
                correlation_id,
                {"force": force, "configuration_version": (config or self.config).version},
            )
            return self.schedule(job_id, force, config, _session=session)

    def _claim(self, run_id: UUID) -> tuple[UUID, ChunkingConfig] | None:
        with self.sessions.begin() as session:
            initial = session.get(ChunkRun, run_id)
            if initial is None:
                return None
            document, version, job = self.locked(session, initial.ingestion_job_id)
            session.refresh(initial, with_for_update=True)
            if initial.status != "PENDING":
                return None
            if (
                document.archived_at
                or job.status != Status.READY_FOR_CHUNKING
                or job.retry_count != initial.generation
            ):
                initial.status = "CANCELLED"
                initial.completed_at = datetime.now(UTC)
                return None
            config = ChunkingConfig.model_validate(initial.config_snapshot)
            token = uuid4()
            initial.status = "RUNNING"
            initial.started_at = datetime.now(UTC)
            initial.lease_token = token
            initial.lease_expires_at = datetime.now(UTC) + timedelta(seconds=config.lease_seconds)
            initial.worker_identity = self.worker
            transition(session, job, version, Status.CHUNKING, None, service=self.worker)
            phase_event("CHUNK_RUN_STARTED", initial.correlation_id, run_id)
            return token, config

    def _fenced(
        self, session: Session, run_id: UUID, token: UUID
    ) -> tuple[ChunkRun, DocumentVersion, IngestionJob]:
        run = session.get(ChunkRun, run_id)
        if run is None:
            raise ChunkCancelled()
        document, version, job = self.locked(session, run.ingestion_job_id)
        session.refresh(run, with_for_update=True)
        parsed = session.get(ParseRun, run.parse_run_id)
        if (
            run.status != "RUNNING"
            or run.lease_token != token
            or not run.lease_expires_at
            or run.lease_expires_at < datetime.now(UTC)
            or job.status not in {Status.CHUNKING, Status.VALIDATING_CHUNKS}
            or job.retry_count != run.generation
            or document.archived_at
        ):
            raise ChunkCancelled()
        if parsed is None or not parsed.is_active:
            raise ChunkError("CHUNK_SOURCE_PARSE_NOT_READY")
        return run, version, job

    def run(self, run_id: UUID) -> None:
        claim = self._claim(run_id)
        if claim is None:
            return
        token, config = claim
        started = time.perf_counter()
        last_check = 0.0

        def checkpoint() -> None:
            nonlocal last_check
            elapsed = time.perf_counter() - started
            if elapsed > config.timeout_seconds:
                raise ChunkError("CHUNK_TIMEOUT")
            if elapsed - last_check >= 1:
                with self.sessions.begin() as session:
                    self._fenced(session, run_id, token)
                last_check = elapsed

        try:
            with self.sessions() as session:
                run = session.get(ChunkRun, run_id)
                assert run is not None
                source = load_source(session, run, config)
                correlation = run.correlation_id
            tokenizer = LocalTokenizer(config)

            def announce(phase: str) -> None:
                phase_event(phase, correlation, run_id)

            dataset = Builder(source, config, tokenizer, checkpoint, announce).build()
            with self.sessions.begin() as session:
                run, version, job = self._fenced(session, run_id, token)
                run.input_fingerprint = dataset.input_fingerprint
                transition(
                    session, job, version, Status.VALIDATING_CHUNKS, None, service=self.worker
                )
            phase_event("CHUNK_VALIDATION_STARTED", correlation, run_id)
            result, findings, metrics = validate(source, dataset, config, tokenizer)
            checkpoint()
            self._persist(
                run_id,
                token,
                source,
                dataset,
                result,
                findings,
                metrics,
                int((time.perf_counter() - started) * 1000),
            )
        except ChunkCancelled:
            self._fail(run_id, token, None, cancelled=True)
        except ChunkError as exc:
            self._fail(run_id, token, exc.code)
        except Exception:
            self._fail(run_id, token, "CHUNK_PERSISTENCE_FAILED")

    def _persist(
        self,
        run_id: UUID,
        token: UUID,
        source: ChunkInput,
        dataset: ChunkDataset,
        result: str,
        findings: list[Finding],
        metrics: dict[str, Any],
        duration: int,
    ) -> None:
        with self.sessions.begin() as session:
            run, version, job = self._fenced(session, run_id, token)
            ids = {c.key: uuid5(run_id, c.key) for c in dataset.chunks}
            qids = {q.key: uuid5(run_id, "question/" + q.key) for q in dataset.questions}
            element_map = {e.id: e for e in source.elements}
            artifact_map = {a.id: a for a in source.artifacts}
            if result != "FAIL":
                for q in dataset.questions:
                    pages = [
                        n
                        for s in q.source_spans
                        if (n := element_map[s.element_id].page_number) is not None
                    ]
                    session.add(
                        QuestionArtifact(
                            id=qids[q.key],
                            chunk_run_id=run_id,
                            parse_run_id=run.parse_run_id,
                            tenant_id=run.tenant_id,
                            question_hash=q.key,
                            question_number=q.number,
                            question_text=q.text,
                            question_type=q.question_type,
                            explicit_answer=q.explicit_answer,
                            explanation=q.explanation,
                            extraction_status=q.extraction_status,
                            structure_inferred=q.structure_inferred,
                            answer_inferred=False,
                            authority=q.authority,
                            page_start=min(pages) if pages else None,
                            page_end=max(pages) if pages else None,
                        )
                    )
                session.flush()
                for q in dataset.questions:
                    for option in q.options:
                        session.add(
                            QuestionOption(
                                question_id=qids[q.key],
                                ordinal=option.ordinal,
                                label=option.label,
                                text=option.text,
                            )
                        )
                for chunk in dataset.chunks:
                    session.add(
                        Chunk(
                            id=ids[chunk.key],
                            tenant_id=run.tenant_id,
                            chunk_run_id=run_id,
                            parse_run_id=run.parse_run_id,
                            parent_chunk_id=ids.get(chunk.parent_key or ""),
                            question_id=qids.get(chunk.question_key or ""),
                            chunk_type=chunk.kind,
                            sequence_number=chunk.sequence,
                            raw_text=chunk.source_text,
                            normalized_text=chunk.source_text,
                            retrieval_text=chunk.retrieval_text,
                            token_count=chunk.token_count,
                            retrieval_token_count=chunk.retrieval_token_count,
                            page_start=chunk.page_start,
                            page_end=chunk.page_end,
                            chunk_hash=chunk.key,
                            chunk_metadata=chunk.metadata,
                        )
                    )
                    session.flush()
                    spans = list(chunk.spans) + [
                        Span(element_id=i, end=len(element_map[i].text), role="HIERARCHY")
                        for i in chunk.hierarchy
                    ]
                    for position, span in enumerate(spans):
                        session.add(
                            ChunkSourceElement(
                                chunk_id=ids[chunk.key],
                                chunk_run_id=run_id,
                                parse_run_id=run.parse_run_id,
                                element_id=span.element_id,
                                position=position,
                                start_offset=span.start,
                                end_offset=span.end,
                                role=span.role,
                            )
                        )
                    for page in {
                        element_map[s.element_id].page_id
                        for s in chunk.spans
                        if element_map[s.element_id].page_id
                    }:
                        session.add(
                            ChunkSourcePage(
                                chunk_id=ids[chunk.key],
                                chunk_run_id=run_id,
                                parse_run_id=run.parse_run_id,
                                page_id=page,
                            )
                        )
                    for artifact_id in chunk.artifact_ids:
                        kind = artifact_map[artifact_id].kind
                        session.add(
                            ChunkArtifactRelation(
                                chunk_id=ids[chunk.key],
                                chunk_run_id=run_id,
                                parse_run_id=run.parse_run_id,
                                table_id=artifact_id if kind == "TABLE" else None,
                                formula_id=artifact_id if kind == "FORMULA" else None,
                                figure_id=artifact_id if kind == "FIGURE" else None,
                            )
                        )
                groups: dict[str, list[UUID]] = defaultdict(list)
                for chunk in dataset.chunks:
                    if chunk.parent_key:
                        groups[chunk.parent_key].append(ids[chunk.key])
                for group in groups.values():
                    for a, b in zip(group, group[1:], strict=False):
                        session.add(
                            ChunkRelation(
                                chunk_run_id=run_id,
                                source_id=a,
                                target_id=b,
                                relation="NEXT_SIBLING",
                            )
                        )
            for f in findings:
                session.add(
                    ChunkValidationFinding(
                        chunk_run_id=run_id,
                        chunk_id=ids.get(f.chunk_key or "") if result != "FAIL" else None,
                        severity=f.severity,
                        code=f.code,
                        message=f.message,
                        details=f.details,
                    )
                )
            session.flush()
            run.validation_result = result
            run.metrics = metrics
            run.duration_ms = duration
            run.completed_at = datetime.now(UTC)
            run.lease_expires_at = None
            if result in {"PASS", "PASS_WITH_WARNINGS"}:
                session.execute(
                    update(ChunkRun)
                    .where(
                        ChunkRun.document_version_id == run.document_version_id,
                        ChunkRun.is_active.is_(True),
                        ChunkRun.id != run.id,
                    )
                    .values(is_active=False)
                )
                run.status = "SUCCEEDED"
                run.is_active = True
                target = Status.READY_FOR_EMBEDDING
            else:
                run.status = "FAILED" if result == "FAIL" else "NEEDS_REVIEW"
                run.is_active = False
                target = Status.FAILED if result == "FAIL" else Status.NEEDS_REVIEW
                run.error_code = (
                    "CHUNK_VALIDATION_FAILED" if result == "FAIL" else "CHUNK_NEEDS_REVIEW"
                )
                run.error_message = MESSAGES[run.error_code][0]
            transition(
                session,
                job,
                version,
                target,
                None,
                service=self.worker,
                error_code=run.error_code,
                error_message=run.error_message,
            )
            audit(
                session,
                run.tenant_id,
                None,
                "CHUNK_RUN_COMPLETED",
                run.id,
                run.correlation_id,
                {"result": result, "chunks": metrics["chunks"]},
            )
            phase_event("CHUNK_DATASET_COMMITTED", run.correlation_id, run.id)

    def _fail(self, run_id: UUID, token: UUID, code: str | None, cancelled: bool = False) -> None:
        with self.sessions.begin() as session:
            initial = session.get(ChunkRun, run_id)
            if initial is None:
                return
            _, version, job = self.locked(session, initial.ingestion_job_id)
            session.refresh(initial, with_for_update=True)
            # Resolve ambiguous commits and fence a superseded attempt.
            if initial.status not in {"PENDING", "RUNNING"} or initial.lease_token != token:
                return
            initial.status = "CANCELLED" if cancelled else "FAILED"
            initial.is_active = False
            initial.completed_at = datetime.now(UTC)
            initial.lease_expires_at = None
            initial.error_code = code
            initial.error_message = MESSAGES[code][0] if code else None
            if (
                job.status in {Status.CHUNKING, Status.VALIDATING_CHUNKS}
                and job.retry_count == initial.generation
            ):
                transition(
                    session,
                    job,
                    version,
                    Status.CANCELLED if cancelled else Status.FAILED,
                    None,
                    service=self.worker,
                    error_code=initial.error_code,
                    error_message=initial.error_message,
                )
            audit(
                session,
                initial.tenant_id,
                None,
                "CHUNK_RUN_FAILED" if not cancelled else "CHUNK_RUN_CANCELLED",
                run_id,
                initial.correlation_id,
                {"code": code},
            )

    def sweep(self) -> int:
        with self.sessions() as session:
            jobs = list(
                session.scalars(
                    select(IngestionJob.id)
                    .where(IngestionJob.status == Status.READY_FOR_CHUNKING)
                    .limit(20)
                )
            )
            expired = list(
                session.execute(
                    select(ChunkRun.id, ChunkRun.lease_token)
                    .where(
                        ChunkRun.status == "RUNNING", ChunkRun.lease_expires_at < datetime.now(UTC)
                    )
                    .limit(20)
                )
            )
        for job_id in jobs:
            self.schedule(job_id)
        for run_id, token in expired:
            self._fail(run_id, token, "CHUNK_LEASE_EXPIRED")
        # Recover the narrow receipt-to-claim crash window without duplicating a claimed run.
        with self.sessions.begin() as session:
            pending = list(
                session.scalars(
                    select(OutboxMessage)
                    .join(ChunkRun, ChunkRun.id == OutboxMessage.chunk_run_id)
                    .where(
                        ChunkRun.status == "PENDING",
                        OutboxMessage.received_at.is_not(None),
                        OutboxMessage.published_at
                        < datetime.now(UTC)
                        - timedelta(seconds=self.config.receipt_recovery_seconds),
                    )
                    .with_for_update(of=OutboxMessage, skip_locked=True)
                    .limit(20)
                )
            )
            for message in pending:
                message.received_at = None
        return len(jobs) + len(expired)
