from uuid import UUID, uuid4

import pytest
from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


class StubProbe:
    def __init__(self, checks: dict[str, bool]) -> None:
        self.checks = checks

    async def check(self) -> dict[str, bool]:
        return self.checks


@pytest.fixture
def config() -> Settings:
    return Settings(environment="test")


def test_liveness_does_not_require_infrastructure(config: Settings) -> None:
    with TestClient(create_app(config, StubProbe({"postgres": False}))) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive", "milestone": "M4"}
    UUID(response.headers["X-Request-ID"])


@pytest.mark.parametrize(
    "checks,code",
    [
        ({"postgres": True, "redis": True, "qdrant": True, "object_storage": True}, 200),
        ({"postgres": False, "redis": True, "qdrant": True, "object_storage": True}, 503),
        ({}, 503),
    ],
)
def test_readiness_is_fail_closed(config: Settings, checks: dict[str, bool], code: int) -> None:
    with TestClient(create_app(config, StubProbe(checks))) as client:
        response = client.get("/health/ready")
    assert response.status_code == code
    assert response.json()["dependencies"] == checks


def test_correlation_and_metrics(config: Settings) -> None:
    with TestClient(create_app(config, StubProbe({}))) as client:
        request_id = str(uuid4())
        assert (
            client.get("/health/live", headers={"X-Request-ID": request_id}).headers["X-Request-ID"]
            == request_id
        )
        invalid = client.get("/health/live", headers={"X-Request-ID": "untrusted-value"})
        UUID(invalid.headers["X-Request-ID"])
        assert 'medrag_http_requests_total{status="200"} 2.0' in client.get("/metrics").text


def test_domain_endpoints_are_not_accidentally_exposed(config: Settings) -> None:
    with TestClient(create_app(config, StubProbe({}))) as client:
        for path in ("/ask", "/documents", "/settings"):
            assert client.post(path, json={"question": "any question"}).status_code == 404


def test_internal_errors_do_not_expose_secrets(config: Settings) -> None:
    class FailingProbe:
        async def check(self) -> dict[str, bool]:
            raise RuntimeError("password=private-value")

    with TestClient(create_app(config, FailingProbe())) as client:
        response = client.get("/health/ready")
    assert response.status_code == 500
    assert "private-value" not in response.text
    UUID(response.headers["X-Request-ID"])
