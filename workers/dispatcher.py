"""Durable outbox dispatcher and upload-intent reconciler, independently restartable."""

import logging
import time

from app.core.config import Settings
from app.db.session import make_engine, make_sessions
from app.observability.ingestion import IngestionMetrics
from app.observability.logging import configure_service_logging
from app.services.parsing import reap_expired_leases
from app.services.queue import dispatch
from app.services.storage import S3ObjectStorage
from app.services.uploads import UploadService
from prometheus_client import CollectorRegistry

from workers.celery_app import CeleryPublisher, create_celery


def main() -> None:
    configure_service_logging()
    settings = Settings()
    engine = make_engine(settings)
    sessions = make_sessions(engine)
    publisher = CeleryPublisher(create_celery(settings))
    uploads = UploadService(
        sessions,
        S3ObjectStorage(settings),
        settings.ingestion,
        IngestionMetrics(CollectorRegistry()),
    )
    try:
        while True:
            try:
                uploads.recover()
                dispatch(sessions, publisher, settings.ingestion)
                # Release parse leases whose worker stopped heartbeating, so a crashed parse
                # becomes a retryable failure instead of a job stuck in PARSING forever.
                reap_expired_leases(sessions, settings.parsing)
            except Exception as exc:
                # No raw broker/SQL/S3 errors; durable rows remain available for the next attempt.
                logging.warning("control_cycle_failed type=%s", type(exc).__name__)
            time.sleep(settings.ingestion.dispatch_interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
