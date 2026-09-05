from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.db.session import make_engine, make_sessions
from app.services.queue import receive
from app.services.storage import S3ObjectStorage
from celery import Celery


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

    @application.task(name="ingestion.receive")
    def receipt(message_id: str) -> None:
        engine = make_engine(config)
        try:
            receive(make_sessions(engine), S3ObjectStorage(config), UUID(message_id))
        finally:
            engine.dispose()

    return application
