"""M12 hardening smoke against the running stack. No provider call unless opted in.

    uv run python scripts/smoke_m12.py

Exercises the controls that only exist end to end: real HTTP through nginx to the API, real
headers on real responses, real authorization on real rows, and the verified-answer rule holding
across the whole chain. Everything here is read-only apart from one Ask, which on a stack without
a configured generator abstains at the gate and costs nothing.

Secrets are never printed. The checks that look for them compare against the configured values and
report only whether a match was found.
"""

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import Settings  # noqa: E402

#: The browser-facing origin. nginx proxies only /api/ to the API and serves the SPA otherwise.
BASE = "http://127.0.0.1:5173"
#: The API's own address. Health, readiness and metrics are deliberately *not* proxied through the
#: public origin: probes and scrape targets are internal surfaces, and exposing them through the
#: browser origin would publish dependency state and metric cardinality to anyone who can load the
#: page. Checking them here is checking them where an orchestrator and Prometheus would.
API = "http://127.0.0.1:8000"
FOREIGN = "11111111-2222-3333-4444-555555555555"


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    print(f"  ok  {label}{(' — ' + detail) if detail else ''}")


def main() -> None:
    settings = Settings(_env_file=str(ROOT / ".env"))
    admin = next(c for c in settings.dev_principals if c.role == "admin")
    reader = next(c for c in settings.dev_principals if c.role == "reader")
    as_admin = {"Authorization": "Bearer " + admin.token.get_secret_value()}
    as_reader = {"Authorization": "Bearer " + reader.token.get_secret_value()}

    with (
        httpx.Client(base_url=BASE, timeout=120) as client,
        httpx.Client(base_url=API, timeout=120) as internal,
    ):
        print("health and readiness (internal surface, not proxied publicly)")
        check(
            "health is not exposed through the public origin",
            "application/json" not in client.get("/health/ready").headers.get("content-type", ""),
        )
        live = internal.get("/health/live")
        check("liveness responds without touching a dependency", live.status_code == 200)
        ready = internal.get("/health/ready")
        check(
            "readiness reports dependency state",
            ready.status_code in {200, 503} and "dependencies" in ready.json(),
            f"status={ready.json()['status']}",
        )
        check(
            "readiness names what is unready",
            "unready" in ready.json(),
            str(ready.json().get("unready") or "all ready"),
        )

        print("security headers")
        for header, expected in (
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ):
            check(
                f"header {header}",
                live.headers.get(header) == expected,
                live.headers.get(header, ""),
            )
        csp = live.headers.get("Content-Security-Policy", "")
        check(
            "CSP forbids framing and objects",
            "frame-ancestors 'none'" in csp and "object-src 'none'" in csp,
        )

        print("authentication")
        check("unauthenticated read refused", client.get("/api/v1/documents").status_code == 401)
        check(
            "garbage bearer refused",
            client.get("/api/v1/documents", headers={"Authorization": "Bearer nope"}).status_code
            == 401,
        )
        check(
            "authenticated read allowed",
            client.get("/api/v1/documents", headers=as_admin).status_code == 200,
        )

        print("authorization and tenant isolation")
        check(
            "reader refused the settings surface",
            client.get("/api/v1/settings", headers=as_reader).status_code == 403,
        )
        check(
            "reader refused the retrieval inspector",
            client.post(
                "/api/v1/retrieval/search", headers=as_reader, json={"query": "x"}
            ).status_code
            == 403,
        )
        check(
            "reader refused operations",
            client.get("/api/v1/operations/status", headers=as_reader).status_code == 403,
        )
        check(
            "admin allowed operations",
            client.get("/api/v1/operations/status", headers=as_admin).status_code == 200,
        )
        for route in (
            f"/api/v1/documents/{FOREIGN}",
            f"/api/v1/ingestion/jobs/{FOREIGN}",
            f"/api/v1/conversations/{FOREIGN}",
            f"/api/v1/chunk-runs/{FOREIGN}",
        ):
            status = client.get(route, headers=as_admin).status_code
            check(f"foreign id refused at {route.split('/')[3]}", status in {403, 404}, str(status))

        print("source authorization")
        listing = client.get("/api/v1/documents", headers=as_admin).json()["items"]
        if listing:
            document = listing[0]
            versions = client.get(
                f"/api/v1/documents/{document['id']}/versions", headers=as_admin
            ).json()["items"]
            if versions:
                source = f"/api/v1/documents/{document['id']}/versions/{versions[0]['id']}/source"
                check(
                    "source refused without authentication", client.get(source).status_code == 401
                )
                authorized = client.get(source, headers=as_admin)
                check("source served to its owner", authorized.status_code == 200)
                check(
                    "source is streamed, not redirected to object storage",
                    not authorized.headers.get("Location"),
                )
        else:
            print("  --  no ingested document; source authorization checked by the test suite")

        print("secret exposure")
        secrets = {
            "database_url": settings.database_url.get_secret_value(),
            "s3_secret_key": settings.s3_secret_key.get_secret_value(),
            "openai_api_key": settings.openai_api_key.get_secret_value(),
        }
        for base, route in (
            (internal, "/health/ready"),
            (internal, "/metrics"),
            (client, "/api/v1/settings"),
            (client, "/api/v1/operations/status"),
        ):
            body = base.get(route, headers=as_admin).text
            leaked = [name for name, value in secrets.items() if value.strip() and value in body]
            check(f"no secret in {route}", not leaked, ",".join(leaked))
        bundle = client.get("/").text
        check(
            "no provider variable compiled into the frontend",
            "VITE_OPENAI" not in bundle and "VITE_ANTHROPIC" not in bundle,
        )

        print("request bounds")
        oversized = client.post(
            "/api/v1/ask", headers=as_reader, content=b'{"question": "' + b"x" * 400_000 + b'"}'
        )
        check("oversized body refused", oversized.status_code == 413, str(oversized.status_code))

        print("the verified answer rule")
        answer = client.post(
            "/api/v1/ask", headers=as_reader, json={"question": "What is the illustrative dose?"}
        )
        check("ask responds", answer.status_code == 200, answer.text[:120])
        body = answer.json()
        check(
            "answer present if and only if VERIFIED",
            (body["answer"] is not None) == (body["outcome"] == "VERIFIED"),
            f"outcome={body['outcome']}",
        )
        check(
            "verified flag agrees with the outcome",
            body["verified"] == (body["outcome"] == "VERIFIED"),
        )
        if body["outcome"] != "VERIFIED":
            check(
                "a refusal carries no citations or sources",
                not body["citations"] and not body["sources"],
            )
        check("no draft field is exposed", "draft" not in body and "sufficiency" not in body)

    print(
        "\nPASS: headers, authentication, RBAC, tenant isolation, source authorization, secret "
        "containment, request bounds and the verified-answer rule all held against the running "
        "stack."
    )


if __name__ == "__main__":
    main()
