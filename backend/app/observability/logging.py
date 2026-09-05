import json
import logging


def configure_service_logging() -> None:
    logger = logging.getLogger("medical_rag.requests")
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJSONFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


class SafeJSONFormatter(logging.Formatter):
    """Only approved operational fields; omit arbitrary messages, exception bodies and input."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "event": getattr(record, "event", "request_complete"),
                "request_id": getattr(record, "request_id", None),
                "resource_id": getattr(record, "resource_id", None),
                "error_type": getattr(record, "error_type", None),
                "status": getattr(record, "status", None),
                "duration_ms": getattr(record, "duration_ms", None),
            }
        )
