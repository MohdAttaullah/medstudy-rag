from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.db.session import make_engine, make_sessions
from app.ingestion.parser.docling_adapter import DoclingDocumentParser
from app.models.documents import OutboxMessage
from app.observability.parsing import ParseMetrics
from app.services.chunking import ChunkService
from app.services.parsing import ParseService
from app.services.queue import receive
from app.services.storage import S3ObjectStorage
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
                run_id = message.chunk_run_id if message else None
            if kind == "CHUNKING" and run_id:
                chunks.run(run_id)
            elif kind == "PARSING":
                parse_service(sessions, storage).run(job_id)
                chunks.schedule(job_id)
        finally:
            engine.dispose()

    return application
