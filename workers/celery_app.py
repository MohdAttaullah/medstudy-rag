from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.db.session import make_engine, make_sessions
from app.embeddings.medcpt import MedCPTArticleEmbedder, configure_offline_environment
from app.ingestion.parser.docling_adapter import DoclingDocumentParser
from app.models.documents import OutboxMessage
from app.observability.parsing import ParseMetrics
from app.services.chunking import ChunkService
from app.services.embedding import EmbeddingService
from app.services.parsing import ParseService
from app.services.queue import receive
from app.services.sparse_index import SparseIndexService
from app.services.storage import S3ObjectStorage
from app.vectorindex.qdrant import QdrantVectorIndex
from celery import Celery
from prometheus_client import CollectorRegistry


class CeleryPublisher:
    def __init__(self, app: Celery) -> None:
        self.app = app

    def publish(self, message_id: UUID) -> None:
        self.app.send_task(
            "ingestion.receive", args=[str(message_id)], task_id=str(message_id), queue="ingestion"
        )


def create_celery(settings: Settings | None = None) -> Any:
    config = settings if settings is not None else Settings()
    if not config.redis_url.get_secret_value():
        raise ValueError("MEDRAG_REDIS_URL is required for the worker")
    application = Celery("medical_rag", broker=config.redis_url.get_secret_value())
    application.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_ignore_result=True,
        task_default_queue="ingestion",
        worker_hijack_root_logger=False,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        broker_connection_timeout=5,
        task_publish_retry=False,
        broker_transport_options={"socket_timeout": 5, "socket_connect_timeout": 5},
        worker_prefetch_multiplier=1,
        # A large textbook must not be able to hold the single worker slot indefinitely. The
        # soft limit raises an exception the task can catch to record a failure; the hard limit
        # kills the process if it does not. Both come from configuration rather than constants,
        # because the right ceiling depends on the size of the documents a deployment ingests.
        task_soft_time_limit=config.parsing.task_soft_timeout_seconds,
        task_time_limit=config.parsing.task_timeout_seconds,
    )
    # The parser holds warm model weights, so it is created once per worker process and only
    # when a task first needs it. Importing this module must never load a parser or a model.
    state: dict[str, Any] = {}

    def parse_service(sessions: Any, storage: Any) -> ParseService:
        parser = state.get("parser")
        if parser is None:
            parser = DoclingDocumentParser()
            state["parser"] = parser
        metrics = state.get("metrics")
        if metrics is None:
            metrics = ParseMetrics(CollectorRegistry())
            state["metrics"] = metrics
        return ParseService(sessions, storage, parser, config.parsing, metrics)

    def embedding_service(sessions: Any) -> EmbeddingService:
        """Embedding model weights stay warm for the life of the worker process.

        Loading MedCPT costs seconds and hundreds of megabytes, so it is built once and only when
        an embedding task first needs it. Importing this module must never load a model.
        """

        def model() -> Any:
            embedder = state.get("embedder")
            if embedder is None:
                configure_offline_environment(config.embedding)
                embedder = MedCPTArticleEmbedder(config.embedding)
                embedder.load()
                state["embedder"] = embedder
            return embedder

        def index() -> Any:
            vector_index = state.get("index")
            if vector_index is None:
                vector_index = QdrantVectorIndex(
                    config.qdrant_url,
                    timeout=config.index.request_timeout_seconds,
                    retries=config.index.upsert_retries,
                )
                state["index"] = vector_index
            return vector_index

        return EmbeddingService(
            sessions, config.embedding, config.index, model_factory=model, index_factory=index
        )

    def sparse_service(sessions: Any) -> SparseIndexService:
        """Lexical indexing needs no model: it is the analyzer, PostgreSQL and nothing else."""
        return SparseIndexService(sessions, config.sparse_analyzer, config.sparse_index)

    @application.task(name="ingestion.receive")
    def receipt(message_id: str) -> None:
        """Confirm durable receipt, then run the M2 parse pipeline for the claimed job.

        `receive` returns a job id for exactly one delivery of a message; a duplicate or stale
        delivery returns None and stops here without touching the parse path.
        """
        engine = make_engine(config)
        try:
            sessions = make_sessions(engine)
            storage = S3ObjectStorage(config)
            job_id = receive(sessions, storage, UUID(message_id))
            if job_id is None:
                return
            chunks = ChunkService(sessions, config.chunking)
            with sessions() as session:
                message = session.get(OutboxMessage, UUID(message_id))
                kind = message.kind if message else None
                chunk_run_id = message.chunk_run_id if message else None
                embedding_run_id = message.embedding_run_id if message else None
                sparse_index_id = message.sparse_index_id if message else None
            if kind == "CHUNKING" and chunk_run_id:
                chunks.run(chunk_run_id)
                embedding_service(sessions).schedule(job_id)
            elif kind == "EMBEDDING" and embedding_run_id:
                embedding_service(sessions).run(embedding_run_id)
                # The lexical lane follows the dense one and is built from its chunk set, so it
                # is scheduled here rather than independently; a dense run that failed leaves
                # the job outside READY_FOR_RETRIEVAL and schedules nothing.
                sparse_service(sessions).schedule(job_id)
            elif kind == "SPARSE_INDEX" and sparse_index_id:
                sparse_service(sessions).run(sparse_index_id)
            elif kind == "PARSING":
                parse_service(sessions, storage).run(job_id)
                chunks.schedule(job_id)
        finally:
            engine.dispose()

    return application
