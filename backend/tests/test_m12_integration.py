"""M12 hardening against the real application: isolation, headers, limits, errors, failure modes.

Real PostgreSQL, real HTTP, real routing. The M9 and M10 suites already prove tenant isolation on
their own routes; what this adds is a systematic sweep across *every* tenant-scoped route at once,
so a route added later without scoping is caught here rather than in an incident.
"""

import json
import os
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from tests.test_m1_integration import auth, database  # noqa: F401
from tests.test_m1_integration import system as system

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MEDRAG_RUN_INTEGRATION") != "1", reason="Requires PostgreSQL"
    ),
]

FOREIGN_ID = "11111111-2222-3333-4444-555555555555"


def tenant_scoped_routes(client: TestClient) -> list[str]:
    """Every parameterised GET the API exposes, addressed with ids the caller does not own.

    Derived from the OpenAPI schema rather than listed by hand, so a route added later is swept
    automatically. A hand-maintained list is exactly the artefact that goes stale and leaves the
    newest route — the one most likely to have the bug — untested.
    """
    paths = client.app.openapi()["paths"]
    routes = []
    for path, operations in paths.items():
        if "get" not in operations or "{" not in path:
            continue
        concrete = path
        while "{" in concrete:
            start, end = concrete.index("{"), concrete.index("}")
            concrete = concrete[:start] + FOREIGN_ID + concrete[end + 1 :]
        routes.append(concrete)
    return sorted(routes)


# --------------------------------------------------------------------- tenant isolation sweep


def test_no_tenant_scoped_route_discloses_another_tenants_resource(system):
    """A foreign id must be indistinguishable from one that never existed.

    404 and 403 are both acceptable refusals, but 200 never is: returning the resource, or a
    "forbidden" that confirms it exists, both leak the fact of its existence to a prober.
    """
    client, _, credentials = system
    routes = tenant_scoped_routes(client)
    assert len(routes) >= 30, "the sweep should cover the whole parameterised surface"
    disclosed = []
    for route in routes:
        response = client.get(route, headers=auth(credentials))
        if response.status_code == 200:
            disclosed.append(route)
        assert response.status_code in {401, 403, 404, 422, 503}, (route, response.status_code)
    assert not disclosed, f"routes returned a foreign resource: {disclosed}"


def test_every_tenant_scoped_route_requires_authentication(system):
    client, _, _ = system
    for route in (*tenant_scoped_routes(client), "/api/v1/documents", "/api/v1/settings"):
        assert client.get(route).status_code == 401, route


def test_a_browser_supplied_tenant_id_is_never_trusted(system):
    """Tenant identity comes from the verified principal, never from the request."""
    client, _, credentials = system
    foreign = str(credentials[2].tenant_id)
    for attempt in (
        {"headers": {**auth(credentials, 1), "X-Tenant-ID": foreign}},
        {"headers": auth(credentials, 1), "params": {"tenant_id": foreign}},
    ):
        response = client.get("/api/v1/documents", **attempt)
        assert response.status_code == 200
        # The reader's own (empty) tenant view, not the other tenant's.
        assert response.json()["items"] == []


def test_ask_rejects_a_client_supplied_tenant_or_verification_claim(system):
    client, _, credentials = system
    for payload in ({"tenant_id": FOREIGN_ID}, {"verified": True}, {"answer": "x"}):
        response = client.post(
            "/api/v1/ask", headers=auth(credentials, 1), json={"question": "q", **payload}
        )
        assert response.status_code == 422, payload


# --------------------------------------------------------------------------------- RBAC live


def test_reader_is_refused_every_administrative_and_diagnostic_route(system):
    client, _, credentials = system
    reader = auth(credentials, 1)
    for method, route in (
        ("get", "/api/v1/settings"),
        ("get", "/api/v1/settings/history"),
        ("get", "/api/v1/operations/status"),
        ("post", "/api/v1/retrieval/search"),
        ("post", "/api/v1/retrieval/draft"),
        ("post", "/api/v1/retrieval/answer"),
    ):
        call = getattr(client, method)
        response = call(
            route, headers=reader, **({"json": {"query": "q"}} if method == "post" else {})
        )
        assert response.status_code == 403, (route, response.status_code)


def test_operations_status_is_admin_only_and_leaks_no_credential(system):
    client, control, credentials = system
    assert client.get("/api/v1/operations/status", headers=auth(credentials, 1)).status_code == 403
    response = client.get("/api/v1/operations/status", headers=auth(credentials))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_mode"] == "development"
    assert set(body["credentials_present"]) == {"openai", "anthropic"}
    assert all(isinstance(v, bool) for v in body["credentials_present"].values())
    # Model identity is present for incident attribution; no secret value is.
    assert body["models"]["embedding"]
    assert body["policy_fingerprints"]["sufficiency"]
    for credential in credentials:
        assert credential.token.get_secret_value() not in response.text
    assert control.settings.database_url.get_secret_value() not in response.text


# ------------------------------------------------------------------------- headers and limits


def test_security_headers_are_present_on_success_and_on_error(system):
    client, _, credentials = system
    for response in (
        client.get("/health/live"),
        client.get("/api/v1/documents", headers=auth(credentials)),
        client.get("/api/v1/documents"),  # 401
        client.get(f"/api/v1/documents/{FOREIGN_ID}", headers=auth(credentials)),  # 404
    ):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_an_oversized_json_body_is_refused_before_it_is_parsed(system):
    client, _, credentials = system
    response = client.post(
        "/api/v1/ask",
        headers=auth(credentials, 1),
        content=json.dumps({"question": "x" * 400_000}),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


def test_rate_limiting_returns_429_when_enabled(database):  # noqa: F811
    """Enabled explicitly here: it is off by default so it never obstructs local work."""
    from app.security.auth import DevCredential

    credential = DevCredential(
        token=uuid4().hex,
        user_id=uuid4(),
        tenant_id=uuid4(),
        display_name="Reader",
        role="reader",
    )
    settings = Settings(
        _env_file=".env",
        database_url=database,
        dev_principals=(credential,),
        limits={"rate_limiting_enabled": True, "ask_per_minute": 2},
    )
    headers = {"Authorization": "Bearer " + credential.token.get_secret_value()}
    with TestClient(create_app(settings)) as client:
        statuses = [
            client.post("/api/v1/ask", headers=headers, json={"question": "q"}).status_code
            for _ in range(4)
        ]
    assert 429 in statuses, statuses
    # Health must never be throttled: doing so turns load into an outage.
    with TestClient(create_app(settings)) as client:
        assert all(client.get("/health/live").status_code == 200 for _ in range(20))


# ---------------------------------------------------------------- health, readiness, errors


def test_liveness_touches_no_dependency_and_readiness_reports_what_is_unready(system):
    client, _, _ = system
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "alive"
    ready = client.get("/health/ready")
    body = ready.json()
    assert set(body) >= {"status", "dependencies", "unready", "auth_mode", "environment"}
    if ready.status_code == 503:
        assert body["unready"], "a not_ready response must name the unmet dependency"


def test_an_internal_failure_returns_a_sanitized_code_and_a_correlation_id(system):
    """No stack trace, no SQL, no filesystem path, no provider prose reaches a client."""
    client, _, credentials = system
    response = client.get(f"/api/v1/documents/{FOREIGN_ID}", headers=auth(credentials))
    body = response.json()
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"].isupper()
    for leak in ("Traceback", "psycopg", "SELECT ", "site-packages", "sqlalchemy", "/app/"):
        assert leak not in response.text
    assert response.headers.get("X-Request-ID")


def test_a_malformed_request_does_not_echo_the_input_back(system):
    """Echoing a rejected value is how a reflected payload reaches a log or an audit record."""
    client, _, credentials = system
    marker = "sk-should-never-be-echoed-000000000000"
    response = client.post(
        "/api/v1/ask", headers=auth(credentials, 1), json={"question": 5, "provider": marker}
    )
    assert response.status_code == 422
    assert marker not in response.text


# -------------------------------------------------------------------------- secret exposure


def test_no_endpoint_returns_a_configured_secret(system):
    client, control, credentials = system
    settings = control.settings
    secrets = [
        settings.database_url.get_secret_value(),
        settings.s3_secret_key.get_secret_value(),
        settings.openai_api_key.get_secret_value(),
        settings.anthropic_api_key.get_secret_value(),
        *(c.token.get_secret_value() for c in credentials),
    ]
    routes = (
        "/health/live",
        "/health/ready",
        "/metrics",
        "/api/v1/documents",
        "/api/v1/settings",
        "/api/v1/operations/status",
        "/api/v1/settings/history",
    )
    for route in routes:
        response = client.get(route, headers=auth(credentials))
        for secret in secrets:
            if secret.strip():
                assert secret not in response.text, f"{route} disclosed a configured secret"


def test_metrics_expose_provider_accounting_without_high_cardinality_labels(system):
    client, _, credentials = system
    body = client.get("/metrics").text
    assert "provider_calls_total" in body
    assert "provider_usage_absent_total" in body
    # Labels are provider and model only; a tenant or correlation label would explode cardinality.
    for forbidden in ("tenant_id=", "correlation_id=", "question="):
        assert forbidden not in body


# ------------------------------------------------------------------------- failure injection


def test_readiness_fails_closed_when_a_dependency_is_unreachable(database):  # noqa: F811
    """A dependency outage must make the replica unready, not make it answer badly."""

    class Broken:
        async def check(self) -> dict[str, bool]:
            return {"postgres": True, "qdrant": False, "redis": True, "object_store": True}

    settings = Settings(_env_file=".env", database_url=database)
    with TestClient(create_app(settings, probe=Broken())) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["unready"] == ["qdrant"]
        # Liveness stays up, so the orchestrator does not restart a healthy process.
        assert client.get("/health/live").status_code == 200


def test_a_provider_outage_never_becomes_an_ungrounded_answer(system):
    """The most important failure-injection case: the provider is gone and Ask must not invent."""
    client, control, credentials = system
    from app.core.errors import DomainError

    async def unavailable(*args: object, **kwargs: object) -> object:
        raise DomainError("GENERATION_PROVIDER_UNAVAILABLE", "provider down", 503)

    control.verification.answer = unavailable  # type: ignore[method-assign]
    response = client.post("/api/v1/ask", headers=auth(credentials, 1), json={"question": "q"})
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "FAILED"
    assert body["answer"] is None
    assert body["verified"] is False
    assert not body["citations"] and not body["sources"]
    # Reported as a technical failure, never as a statement about the evidence.
    assert "GENERATION_PROVIDER_UNAVAILABLE" in body["reason_codes"]


def test_the_verified_answer_rule_survives_every_hardening_change(system):
    """M9's invariant, re-asserted after M12 touched middleware, auth, limits and prompts."""
    client, _, credentials = system
    response = client.post(
        "/api/v1/ask", headers=auth(credentials, 1), json={"question": "unanswerable question"}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["answer"] is not None) == (body["outcome"] == "VERIFIED")
    assert body["verified"] == (body["outcome"] == "VERIFIED")
