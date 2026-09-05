import asyncio
import json
import logging
from unittest.mock import AsyncMock

from app.core.config import Settings
from app.observability.logging import SafeJSONFormatter
from app.services.health import InfrastructureProbe


def test_dependency_failures_are_isolated(monkeypatch) -> None:
    probe = InfrastructureProbe(Settings(environment="test"))

    def broken_database() -> bool:
        raise RuntimeError("secret connection string")

    monkeypatch.setattr(probe, "_postgres", broken_database)
    monkeypatch.setattr(probe, "_storage", lambda: True)
    monkeypatch.setattr(probe, "_redis", AsyncMock(return_value=True))
    monkeypatch.setattr(probe, "_qdrant", AsyncMock(return_value=False))
    assert asyncio.run(probe.check()) == {
        "postgres": False,
        "redis": True,
        "qdrant": False,
        "object_storage": True,
    }


def test_logging_omits_arbitrary_message_and_extras() -> None:
    record = logging.LogRecord(
        "medical_rag.requests", logging.INFO, "", 0, "private question", (), None
    )
    record.provider_key = "private-key"
    result = SafeJSONFormatter().format(record)
    assert "private" not in result
    assert json.loads(result)["event"] == "request_complete"
