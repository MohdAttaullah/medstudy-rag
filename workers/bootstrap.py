"""Celery CLI entrypoint; importing domain modules does not connect to a broker."""

from app.observability.logging import configure_service_logging

from workers.celery_app import create_celery

configure_service_logging()

app = create_celery()
