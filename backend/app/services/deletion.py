"""Permanent deletion of a document and everything derived from it (ADR-026).

Archiving withdraws a document and keeps every byte. Deletion removes the content: the original
file and every parse artifact in object storage, every vector in the index, and every row that
holds text, structure or a pointer to either. What remains afterwards is deliberately small and
contentless — the audit events and a tombstone row naming the document id, who deleted it and when.

Three stores share no transaction, so the work is ordered to be safe at every point of failure:

1. **Withdraw** (one transaction). The document is archived and its jobs cancelled — exactly what
   archiving already does, so it leaves retrieval at once and no reprocessing path can start. A
   tombstone row records the request. If anything later fails, the document stays withdrawn and a
   retry resumes from here.
2. **External cleanup.** Every object version under the document's storage prefix, then every
   vector carrying its tenant and document id. Both are idempotent, and both run *before* the
   relational purge, while PostgreSQL still holds the manifest that says what to clean. Doing it in
   the other order would leave orphaned content nobody could find again.
3. **Purge** (one transaction). Rows are deleted children-first through the guards, which permit
   it only for this document inside this transaction (see the `document_purge` migration). Citations
   to the document keep their identifiers but lose their excerpt and title. The tombstone becomes
   COMPLETED and the deletion is audited.

A document that is still being processed cannot be deleted: a worker holding a live lease could
write an artifact after the cleanup and recreate content the user asked to remove.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import DomainError
from app.ingestion.state import FAILURE_STATES, transition
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
from app.models.conversations import TurnCitation
from app.models.documents import (
    Document,
    DocumentDeletion,
    DocumentVersion,
    IngestionJob,
    IngestionStageEvent,
    OutboxMessage,
    UploadIntent,
)
from app.models.embeddings import ChunkEmbedding, EmbeddingRun, IndexRun, IndexValidationFinding
from app.models.enums import Status
from app.models.parsing import ParseReviewDecision, ParseRun, ParseValidationFinding
from app.models.retrieval import (
    SparseDocument,
    SparseIndex,
    SparsePosting,
    SparseTerm,
    SparseValidationFinding,
)
from app.observability.ingestion import audit
from app.repositories.documents import get_document
from app.schemas.lifecycle import DeletionPreview
from app.security.auth import Principal
from app.services.storage import ObjectStorage, document_prefix
from app.vectorindex.model import VectorIndex, VectorSchema

logger = logging.getLogger("medical_rag.deletion")

#: A request that has been REQUESTED for longer than this without finishing is assumed to have
#: died with its process, and may be resumed. Shorter than this, a second request is refused so two
#: purges of the same document cannot interleave.
STALE_REQUEST = timedelta(minutes=15)
#: A run with no lease that has not been touched for this long is not being worked on.
STALE_RUN = timedelta(minutes=15)
#: Shown in place of a deleted document's title on the citations that referred to it.
DELETED_TITLE = "Deleted source"
TERMINAL = frozenset({Status.RETRIEVAL_READY, *FAILURE_STATES})


class DeletionFailed(Exception):
    """A cleanup step failed; carries a code safe to store and show, never a storage key."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DocumentDeletionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        storage: ObjectStorage,
        vector_index: VectorIndex | None,
        schema_for: Callable[[str], VectorSchema] | None,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.vector_index = vector_index
        self.schema_for = schema_for

    # ---------------------------------------------------------------------------------- preview

    def preview(self, actor: Principal, document_id: UUID) -> DeletionPreview:
        """Count what a deletion would remove, without changing anything."""
        actor.require("document:delete")
        with self.sessions() as session:
            document = get_document(session, actor.tenant_id, document_id)
            versions = self._versions(document)
            chunk_runs = select(ChunkRun.id).where(ChunkRun.document_id == document.id)
            embedding_runs = select(EmbeddingRun.id).where(EmbeddingRun.document_id == document.id)
            return DeletionPreview(
                document_id=document.id,
                versions=int(
                    session.scalar(select(func.count()).select_from(versions.subquery())) or 0
                ),
                pages=int(
                    session.scalar(
                        select(func.coalesce(func.sum(DocumentVersion.page_count), 0)).where(
                            DocumentVersion.document_id == document.id,
                            DocumentVersion.tenant_id == document.tenant_id,
                        )
                    )
                    or 0
                ),
                chunks=int(
                    session.scalar(select(func.count()).where(Chunk.chunk_run_id.in_(chunk_runs)))
                    or 0
                ),
                vectors=int(
                    session.scalar(
                        select(func.count()).where(
                            ChunkEmbedding.embedding_run_id.in_(embedding_runs)
                        )
                    )
                    or 0
                ),
                citing_answers=int(
                    session.scalar(
                        select(func.count(func.distinct(TurnCitation.turn_id))).where(
                            TurnCitation.tenant_id == document.tenant_id,
                            TurnCitation.document_id == document.id,
                            TurnCitation.source_deleted_at.is_(None),
                        )
                    )
                    or 0
                ),
                blocked_reason="PROCESSING" if self._processing(session, document) else None,
            )

    # ----------------------------------------------------------------------------------- delete

    def delete(self, actor: Principal, document_id: UUID, correlation_id: UUID) -> None:
        actor.require("document:delete")
        collections = self._withdraw(actor, document_id, correlation_id)
        if collections is None:
            return  # already deleted; deleting again is a no-op, not an error
        try:
            objects, vectors = self._clean_external(actor.tenant_id, document_id, collections)
            self._purge(actor, document_id, correlation_id, objects, vectors)
        except DeletionFailed as failure:
            self._record_failure(actor, document_id, correlation_id, failure.code)
            raise DomainError(
                "DOCUMENT_DELETE_INCOMPLETE",
                "The document is withdrawn from search, but permanent deletion did not finish. "
                "Nothing was left searchable. Try deleting it again.",
                503,
            ) from failure

    # ------------------------------------------------------------------------- step 1: withdraw

    def _withdraw(
        self, actor: Principal, document_id: UUID, correlation_id: UUID
    ) -> list[str] | None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            tombstone = session.scalar(
                select(DocumentDeletion)
                .where(
                    DocumentDeletion.tenant_id == actor.tenant_id,
                    DocumentDeletion.document_id == document_id,
                )
                .with_for_update()
            )
            if tombstone is not None and tombstone.status == "COMPLETED":
                return None
            # 404 for a document in another tenant — the same answer as for one that never existed.
            document = get_document(session, actor.tenant_id, document_id, lock=True)
            if (
                tombstone is not None
                and tombstone.status == "REQUESTED"
                and tombstone.updated_at > now - STALE_REQUEST
            ):
                raise DomainError(
                    "DOCUMENT_DELETION_IN_PROGRESS",
                    "This document is already being deleted.",
                    409,
                )
            if self._processing(session, document):
                raise DomainError(
                    "DOCUMENT_PROCESSING",
                    "This document is still being processed. Cancel processing and wait for it to "
                    "stop, then delete it.",
                    409,
                )
            # Withdraw exactly as archiving does: out of retrieval now, and no job left that a
            # reprocessing request could restart while the cleanup runs.
            if document.archived_at is None:
                document.archived_at = now
            for version in session.scalars(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.tenant_id == document.tenant_id,
                )
            ):
                if version.archived_at is None:
                    version.archived_at = now
                for job in session.scalars(
                    select(IngestionJob)
                    .where(IngestionJob.document_version_id == version.id)
                    .with_for_update()
                ):
                    if job.status != Status.CANCELLED:
                        job.correlation_id = correlation_id
                        transition(session, job, version, Status.CANCELLED, actor.user_id)
            if tombstone is None:
                tombstone = DocumentDeletion(
                    tenant_id=actor.tenant_id,
                    document_id=document_id,
                    requested_by_user_id=actor.user_id,
                    status="REQUESTED",
                    requested_at=now,
                    attempts=1,
                    correlation_id=correlation_id,
                    removed={},
                )
                session.add(tombstone)
            else:
                tombstone.status = "REQUESTED"
                tombstone.attempts += 1
                tombstone.requested_by_user_id = actor.user_id
                tombstone.correlation_id = correlation_id
                tombstone.last_error_code = None
            audit(
                session,
                actor.tenant_id,
                actor.user_id,
                "DOCUMENT_DELETE_REQUESTED",
                document_id,
                correlation_id,
                {"attempt": tombstone.attempts},
            )
            return sorted(
                {
                    str(name)
                    for name in session.scalars(
                        select(IndexRun.physical_collection).where(
                            IndexRun.document_id == document_id,
                            IndexRun.tenant_id == actor.tenant_id,
                        )
                    )
                    if name
                }
            )

    # ------------------------------------------------------------------- step 2: other stores

    def _clean_external(
        self, tenant_id: UUID, document_id: UUID, collections: list[str]
    ) -> tuple[int, int]:
        try:
            objects = self.storage.delete_prefix(document_prefix(document_id))
        except Exception as exc:
            logger.warning(
                "document_delete_storage_failed",
                extra={"event": "document_delete_storage_failed", "error_type": type(exc).__name__},
            )
            raise DeletionFailed("STORAGE_DELETE_FAILED") from exc
        vectors = 0
        if collections:
            if self.vector_index is None or self.schema_for is None:
                # Vectors exist for this document and nothing here can reach them. Purging the rows
                # now would orphan them for good, so stop and leave the work resumable.
                raise DeletionFailed("VECTOR_INDEX_UNAVAILABLE")
            selector = {"tenant_id": str(tenant_id), "document_id": str(document_id)}
            for collection in collections:
                schema = self.schema_for(collection)
                try:
                    vectors += self.vector_index.delete(schema, selector)
                    remaining = self.vector_index.count(schema, selector)
                except Exception as exc:
                    logger.warning(
                        "document_delete_vectors_failed",
                        extra={
                            "event": "document_delete_vectors_failed",
                            "error_type": type(exc).__name__,
                        },
                    )
                    raise DeletionFailed("VECTOR_DELETE_FAILED") from exc
                if remaining:
                    raise DeletionFailed("VECTOR_DELETE_INCOMPLETE")
        return objects, vectors

    # ---------------------------------------------------------------------- step 3: the purge

    def _purge(
        self,
        actor: Principal,
        document_id: UUID,
        correlation_id: UUID,
        objects: int,
        vectors: int,
    ) -> None:
        try:
            with self.sessions.begin() as session:
                # Transaction-local: the guards admit deletes for this document only, and only
                # until this transaction ends.
                session.execute(
                    text("SELECT set_config('medrag.purge_document', :document, true)"),
                    {"document": str(document_id)},
                )
                tombstone = session.scalar(
                    select(DocumentDeletion)
                    .where(
                        DocumentDeletion.tenant_id == actor.tenant_id,
                        DocumentDeletion.document_id == document_id,
                    )
                    .with_for_update()
                )
                document = session.scalar(
                    select(Document)
                    .where(Document.id == document_id, Document.tenant_id == actor.tenant_id)
                    .with_for_update()
                )
                removed: dict[str, Any] = {"objects": objects, "vectors": vectors}
                if document is not None:
                    removed |= self._delete_rows(session, document)
                if tombstone is not None:
                    tombstone.status = "COMPLETED"
                    tombstone.completed_at = datetime.now(UTC)
                    tombstone.removed = removed
                audit(
                    session,
                    actor.tenant_id,
                    actor.user_id,
                    "DOCUMENT_DELETED",
                    document_id,
                    correlation_id,
                    removed,
                )
        except DeletionFailed:
            raise
        except Exception as exc:
            logger.warning(
                "document_delete_purge_failed",
                extra={"event": "document_delete_purge_failed", "error_type": type(exc).__name__},
            )
            raise DeletionFailed("DATABASE_PURGE_FAILED") from exc

    def _delete_rows(self, session: Session, document: Document) -> dict[str, int]:
        """Delete every row the document owns, children before parents. Returns counts."""
        tenant, doc = document.tenant_id, document.id
        versions = select(DocumentVersion.id).where(
            DocumentVersion.document_id == doc, DocumentVersion.tenant_id == tenant
        )
        jobs = select(IngestionJob.id).where(IngestionJob.document_version_id.in_(versions))
        parses = select(ParseRun.id).where(ParseRun.document_version_id.in_(versions))
        chunk_runs = select(ChunkRun.id).where(ChunkRun.document_id == doc)
        embedding_runs = select(EmbeddingRun.id).where(EmbeddingRun.document_id == doc)
        index_runs = select(IndexRun.id).where(IndexRun.document_id == doc)
        sparse = select(SparseIndex.id).where(SparseIndex.document_id == doc)
        questions = select(QuestionArtifact.id).where(QuestionArtifact.chunk_run_id.in_(chunk_runs))
        counts: dict[str, int] = {}

        def remove(label: str, statement: Any) -> None:
            result = cast(CursorResult[Any], session.execute(statement))
            counts[label] = counts.get(label, 0) + int(result.rowcount or 0)

        # Citations first: the history stays, the deleted source content does not.
        remove(
            "citations_redacted",
            update(TurnCitation)
            .where(
                TurnCitation.tenant_id == tenant,
                TurnCitation.document_id == doc,
                TurnCitation.source_deleted_at.is_(None),
            )
            .values(
                cited_text="",
                document_title=DELETED_TITLE,
                pages=[],
                source_element_ids=[],
                source_spans=[],
                artifacts=[],
                source_deleted_at=datetime.now(UTC),
            ),
        )
        remove(
            "outbox_messages",
            delete(OutboxMessage).where(
                or_(
                    OutboxMessage.job_id.in_(jobs),
                    OutboxMessage.chunk_run_id.in_(chunk_runs),
                    OutboxMessage.embedding_run_id.in_(embedding_runs),
                    OutboxMessage.sparse_index_id.in_(sparse),
                )
            ),
        )
        # Lexical index.
        remove(
            "findings",
            delete(SparseValidationFinding).where(
                SparseValidationFinding.sparse_index_id.in_(sparse)
            ),
        )
        remove(
            "lexical_rows", delete(SparseDocument).where(SparseDocument.sparse_index_id.in_(sparse))
        )
        remove(
            "lexical_rows", delete(SparsePosting).where(SparsePosting.sparse_index_id.in_(sparse))
        )
        remove("lexical_rows", delete(SparseTerm).where(SparseTerm.sparse_index_id.in_(sparse)))
        remove("lexical_indexes", delete(SparseIndex).where(SparseIndex.id.in_(sparse)))
        # Dense index metadata and embeddings.
        remove(
            "findings",
            delete(IndexValidationFinding).where(
                or_(
                    IndexValidationFinding.embedding_run_id.in_(embedding_runs),
                    IndexValidationFinding.index_run_id.in_(index_runs),
                )
            ),
        )
        remove("index_runs", delete(IndexRun).where(IndexRun.id.in_(index_runs)))
        remove(
            "embeddings",
            delete(ChunkEmbedding).where(ChunkEmbedding.embedding_run_id.in_(embedding_runs)),
        )
        remove("embedding_runs", delete(EmbeddingRun).where(EmbeddingRun.id.in_(embedding_runs)))
        # Chunk datasets.
        remove(
            "findings",
            delete(ChunkValidationFinding).where(
                ChunkValidationFinding.chunk_run_id.in_(chunk_runs)
            ),
        )
        for model in (ChunkArtifactRelation, ChunkRelation, ChunkSourceElement, ChunkSourcePage):
            remove("chunk_links", delete(model).where(model.chunk_run_id.in_(chunk_runs)))
        remove(
            "chunk_links", delete(QuestionOption).where(QuestionOption.question_id.in_(questions))
        )
        remove("chunks", delete(Chunk).where(Chunk.chunk_run_id.in_(chunk_runs)))
        remove("questions", delete(QuestionArtifact).where(QuestionArtifact.id.in_(questions)))
        remove("chunk_runs", delete(ChunkRun).where(ChunkRun.id.in_(chunk_runs)))
        # Parses. Pages, elements, tables, figures and formulas cascade from the parse run; the
        # append-only children are removed explicitly first so their guard can still resolve the
        # parent it checks.
        remove(
            "findings",
            delete(ParseReviewDecision).where(ParseReviewDecision.parse_run_id.in_(parses)),
        )
        remove(
            "findings",
            delete(ParseValidationFinding).where(ParseValidationFinding.parse_run_id.in_(parses)),
        )
        remove("parse_runs", delete(ParseRun).where(ParseRun.id.in_(parses)))
        # Jobs and their history, the upload records, the versions and the document itself.
        remove(
            "job_events",
            delete(IngestionStageEvent).where(IngestionStageEvent.ingestion_job_id.in_(jobs)),
        )
        remove("jobs", delete(IngestionJob).where(IngestionJob.id.in_(jobs)))
        remove(
            "upload_intents",
            delete(UploadIntent).where(
                UploadIntent.tenant_id == tenant, UploadIntent.document_id == doc
            ),
        )
        remove("versions", delete(DocumentVersion).where(DocumentVersion.id.in_(versions)))
        remove(
            "documents", delete(Document).where(Document.id == doc, Document.tenant_id == tenant)
        )
        return counts

    # ---------------------------------------------------------------------------------- failure

    def _record_failure(
        self, actor: Principal, document_id: UUID, correlation_id: UUID, code: str
    ) -> None:
        with self.sessions.begin() as session:
            tombstone = session.scalar(
                select(DocumentDeletion).where(
                    DocumentDeletion.tenant_id == actor.tenant_id,
                    DocumentDeletion.document_id == document_id,
                )
            )
            if tombstone is not None:
                tombstone.status = "FAILED"
                tombstone.last_error_code = code
            audit(
                session,
                actor.tenant_id,
                actor.user_id,
                "DOCUMENT_DELETE_FAILED",
                document_id,
                correlation_id,
                {"error_code": code},
            )

    # ---------------------------------------------------------------------------------- helpers

    @staticmethod
    def _versions(document: Document) -> Any:
        return select(DocumentVersion.id).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.tenant_id == document.tenant_id,
        )

    @staticmethod
    def _processing(session: Session, document: Document) -> bool:
        """Whether any job for the document is mid-pipeline, or any worker still holds its work.

        A cancelled job is not proof the worker stopped: a parse notices cancellation only at its
        next fence. So a run is also treated as live while its lease is current — or, for a run
        without a lease, while it was touched recently.
        """
        now = datetime.now(UTC)
        versions = DocumentDeletionService._versions(document)
        statuses = session.scalars(
            select(IngestionJob.status).where(IngestionJob.document_version_id.in_(versions))
        )
        if any(Status(status) not in TERMINAL for status in statuses):
            return True
        live: list[tuple[Any, tuple[str, ...], Any]] = [
            (ParseRun, ("RUNNING",), ParseRun.document_version_id.in_(versions)),
            (ChunkRun, ("PENDING", "RUNNING"), ChunkRun.document_id == document.id),
            (EmbeddingRun, ("PENDING", "RUNNING"), EmbeddingRun.document_id == document.id),
            (SparseIndex, ("STAGING", "VERIFYING"), SparseIndex.document_id == document.id),
        ]
        for model, active, scope in live:
            if session.scalar(
                select(func.count()).where(
                    scope,
                    model.status.in_(active),
                    or_(
                        model.lease_expires_at > now,
                        (model.lease_expires_at.is_(None)) & (model.updated_at > now - STALE_RUN),
                    ),
                )
            ):
                return True
        return False
