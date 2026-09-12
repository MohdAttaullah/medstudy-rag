"""Non-destructive production readiness checks.

    uv run python scripts/production_preflight.py
    uv run python scripts/production_preflight.py --json .local/preflight.json

Answers one question: if this configuration were deployed, would it start, and would it be safe?
Everything here reads; nothing writes, migrates, uploads or deletes. No provider call is made
unless `--check-provider` is passed explicitly, because a preflight that quietly spends money is
one nobody runs before a deploy.

Output is sanitized by construction. Secrets are reported as present or absent and never printed,
and connection strings are reduced to scheme and host so a typo is diagnosable without the
password being pasted into a ticket.

Exit codes: 0 all checks passed, 1 a blocking check failed, 2 the configuration itself is invalid.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.core.secrets import resolve_secret_files, secret_sources  # noqa: E402

OK, WARN, FAIL = "PASS", "WARN", "FAIL"


def _endpoint(raw: str) -> str:
    """Scheme and host only. A password in a connection string never reaches the output."""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "unparseable"
    if not parsed.scheme:
        return "unset" if not raw else "no-scheme"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname or '?'}{port}"


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, status: str, detail: str, **extra: Any) -> None:
        self.checks.append({"check": name, "status": status, "detail": detail, **extra})

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if c["status"] == FAIL]

    @property
    def warned(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if c["status"] == WARN]

    def render(self) -> str:
        width = max((len(c["check"]) for c in self.checks), default=10)
        lines = [f"{c['status']:<4} {c['check']:<{width}}  {c['detail']}" for c in self.checks]
        return "\n".join(lines)


def check_configuration(report: Report) -> Any:
    """Construct Settings. Its own validators are the real production gate; this reports them."""
    from app.core.config import Settings

    try:
        settings = Settings(_env_file=str(ROOT / ".env") if (ROOT / ".env").exists() else None)
    except Exception as exc:  # noqa: BLE001 - a config error is the answer, not a crash
        report.add("configuration", FAIL, "Settings could not be constructed")
        for line in str(exc).splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                report.add("configuration.rule", FAIL, stripped[2:])
        return None
    report.add(
        "configuration",
        OK,
        f"loaded; environment={settings.environment}",
        environment=settings.environment,
    )
    return settings


def check_identity(report: Report, settings: Any) -> None:
    auth = settings.auth
    if settings.environment == "production" and auth.mode != "oidc":
        report.add("identity.mode", FAIL, "production requires OIDC authentication")
    elif auth.mode == "oidc":
        report.add(
            "identity.mode",
            OK,
            f"oidc; issuer={_endpoint(auth.issuer)}, {len(auth.role_mapping)} roles mapped",
        )
        if not auth.jwks_uri.startswith("https://"):
            report.add("identity.jwks", FAIL, "JWKS URI must be HTTPS")
        else:
            report.add("identity.jwks", OK, _endpoint(auth.jwks_uri))
    else:
        report.add(
            "identity.mode",
            WARN if settings.environment != "production" else FAIL,
            "development bearer identities (never valid in production)",
        )
    if settings.dev_principals:
        status = FAIL if settings.environment == "production" else WARN
        report.add(
            "identity.dev_principals",
            status,
            f"{len(settings.dev_principals)} static development identities configured",
        )
    else:
        report.add("identity.dev_principals", OK, "none")


def check_secrets(report: Report, settings: Any) -> None:
    sources = secret_sources()
    for name in ("MEDRAG_DATABASE_URL", "MEDRAG_S3_SECRET_KEY", "OPENAI_API_KEY"):
        origin = sources.get(name, "absent")
        # "absent from the process environment" is not the same as unset: a developer's values
        # arrive from an ignored .env that pydantic reads directly. Say which was observed.
        report.add(
            f"secret.{name.lower()}",
            OK,
            "mounted file"
            if origin == "file"
            else "process environment"
            if origin == "environment"
            else "not in the process environment (a local .env may still supply it)",
        )
    # A configured provider without its key fails every Ask at request time.
    for role in ("generator", "verifier"):
        selection = getattr(settings, role)
        if selection is None:
            report.add(f"provider.{role}", WARN, "not configured; generation unavailable")
        elif settings.credential_for(selection.provider):
            report.add(f"provider.{role}", OK, f"{selection.provider}:{selection.model_id}")
        else:
            report.add(
                f"provider.{role}",
                FAIL,
                f"{selection.provider} configured but its API key is absent",
            )


def check_network(report: Report, settings: Any) -> None:
    production = settings.environment == "production"
    for name, raw in (
        ("qdrant", settings.qdrant_url),
        ("object_store", settings.s3_endpoint),
        ("database", settings.database_url.get_secret_value()),
        ("redis", settings.redis_url.get_secret_value()),
    ):
        endpoint = _endpoint(raw)
        local = any(host in raw for host in ("127.0.0.1", "localhost", "0.0.0.0"))
        status = FAIL if (production and local) else OK
        report.add(f"network.{name}", status, endpoint)
    origins = settings.cors_origins
    if any(o.strip() == "*" for o in origins):
        report.add("network.cors", FAIL, "wildcard origin on a credentialed API")
    elif production and any(o.startswith("http://") for o in origins):
        report.add("network.cors", FAIL, "production origins must be HTTPS")
    else:
        report.add("network.cors", OK, f"{len(origins)} explicit origin(s)")


def check_limits(report: Report, settings: Any) -> None:
    limits = settings.limits
    status = (
        FAIL if (settings.environment == "production" and not limits.rate_limiting_enabled) else OK
    )
    report.add(
        "limits.rate_limiting",
        status,
        "enabled" if limits.rate_limiting_enabled else "disabled",
    )
    report.add("limits.request_size", OK, f"{limits.max_json_body_bytes} byte JSON ceiling")
    report.add(
        "limits.hsts",
        OK if limits.hsts_enabled or settings.environment != "production" else WARN,
        "enabled" if limits.hsts_enabled else "disabled (set where TLS termination is controlled)",
    )


def check_upload_limits(report: Report, settings: Any) -> None:
    """The application limit and the reverse-proxy limit must agree.

    A proxy ceiling below the application's turns an acceptable upload into an opaque 413 that
    never reaches the application, which is exactly how a 153 MiB textbook was rejected while the
    configured limit said 128 MiB. Both are read from the environment the deployment supplies, so
    a mismatch is caught before it reaches a user.
    """
    config = settings.ingestion
    app_bytes = config.max_upload_bytes
    report.add(
        "upload.application_limit",
        OK,
        f"{app_bytes // (1024 * 1024)} MiB per file (and per request: one PDF per request)",
    )

    raw = os.environ.get("MEDRAG_MAX_UPLOAD_MB", "").strip()
    if not raw:
        report.add(
            "upload.proxy_limit",
            WARN,
            "MEDRAG_MAX_UPLOAD_MB is unset; the proxy falls back to its image default. Set it "
            "from the same value as the application limit.",
        )
        return
    try:
        proxy_bytes = int(raw) * 1024 * 1024
    except ValueError:
        report.add("upload.proxy_limit", FAIL, f"MEDRAG_MAX_UPLOAD_MB is not a number: {raw!r}")
        return
    if proxy_bytes < app_bytes:
        report.add(
            "upload.proxy_limit",
            FAIL,
            f"proxy allows {raw} MiB but the application accepts "
            f"{app_bytes // (1024 * 1024)} MiB; the proxy would reject uploads the application "
            "would have taken",
        )
    else:
        headroom = (proxy_bytes - app_bytes) // (1024 * 1024)
        report.add(
            "upload.proxy_limit",
            OK,
            f"proxy allows {raw} MiB, {headroom} MiB above the application limit",
        )

    timeout = config.upload_timeout_seconds
    proxy_timeout = os.environ.get("MEDRAG_UPLOAD_TIMEOUT_SECONDS", "").strip()
    if proxy_timeout.isdigit() and int(proxy_timeout) < timeout:
        report.add(
            "upload.proxy_timeout",
            FAIL,
            f"proxy read timeout {proxy_timeout}s is below the application's {timeout}s; a slow "
            "large upload would be cut off by the proxy",
        )
    else:
        report.add("upload.proxy_timeout", OK, f"application allows {timeout}s per upload")


def check_worker_bounds(report: Report, settings: Any) -> None:
    """Accepting a large upload is not the same as being able to parse it."""
    parsing = settings.parsing
    report.add(
        "worker.parse_timeout",
        OK,
        f"one call {parsing.timeout_seconds}s < document "
        f"{parsing.document_timeout_for(parsing.max_pages)}s < soft task "
        f"{parsing.task_soft_timeout_seconds}s < hard task {parsing.task_timeout_seconds}s",
    )
    report.add("worker.max_pages", OK, f"{parsing.max_pages} pages per document")
    windowed = parsing.page_window_size > 0
    report.add(
        "worker.page_windows",
        OK if windowed else WARN,
        f"{parsing.page_window_size} pages per conversion call above "
        f"{parsing.page_window_threshold} pages"
        if windowed
        else "windowing disabled; a long document converts in one call and peak memory grows "
        "linearly with page count",
    )
    report.add(
        "worker.concurrency",
        OK if parsing.max_concurrency == 1 else WARN,
        f"{parsing.max_concurrency} concurrent parse(s) per worker"
        + ("" if parsing.max_concurrency == 1 else "; models are memory-heavy, prefer 1 per pod"),
    )


def check_models(report: Report, settings: Any) -> None:
    """Model identity and offline pinning. Never downloads and never loads weights."""
    for name, policy in (
        ("embedding", settings.embedding),
        ("query_encoder", settings.query_encoder),
        ("reranker", settings.reranker),
    ):
        revision = getattr(policy, "model_revision", "")
        offline = getattr(policy, "offline", True)
        # Downloading is legitimate while provisioning on a workstation; in production it means a
        # model revision could change under a running service.
        if not revision:
            status = FAIL
        elif offline:
            status = OK
        else:
            status = FAIL if settings.environment == "production" else WARN
        report.add(
            f"model.{name}",
            status,
            f"{policy.model_id}@{revision[:12]} offline={offline}",
        )
    cache = ROOT / ".local" / "models"
    report.add(
        "model.cache",
        OK if cache.exists() else WARN,
        f"{cache} {'present' if cache.exists() else 'absent (provision before serving)'}",
    )


def check_safety_invariants(report: Report, settings: Any) -> None:
    """The M9/M10 invariants that must survive any deployment configuration."""
    checks = {
        "ask.requires_verified_pass": settings.ask.requires_verified_pass is True,
        "ask.stream_answer_tokens": settings.ask.stream_answer_tokens is False,
        "grounding.pretrained_knowledge_is_evidence": (
            settings.grounding.pretrained_knowledge_is_evidence is False
        ),
        "claim_verification.verifier_required": settings.claim_verification.verifier_required,
        "final_verification.verifier_failure_abstains": (
            settings.final_verification.verifier_failure_abstains
        ),
        "retrieval.degradation_policy": settings.retrieval.degradation_policy == "FAIL_CLOSED",
    }
    for name, holds in checks.items():
        report.add(f"safety.{name}", OK if holds else FAIL, "holds" if holds else "VIOLATED")


def check_provider_reachable(report: Report, settings: Any) -> None:
    """Opt-in. Makes one minimal live call to confirm credentials and connectivity."""
    if settings.generator is None:
        report.add("provider.reachable", WARN, "no generator configured; skipped")
        return
    import asyncio

    from app.core.errors import DomainError
    from app.generation.providers.factory import build_provider

    try:
        provider = build_provider(settings)
        asyncio.run(
            provider.generate(
                system_policy="Reply with the single word: ready.",
                question="ready?",
                evidence="(preflight connectivity probe; no corpus content)",
            )
        )
        report.add("provider.reachable", OK, f"{settings.generator.provider} responded")
    except DomainError as exc:
        report.add("provider.reachable", FAIL, f"declared failure {exc.code}")
    except Exception as exc:  # noqa: BLE001
        report.add("provider.reachable", FAIL, f"{type(exc).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="Write the sanitized report here.")
    parser.add_argument(
        "--check-provider",
        action="store_true",
        help="Additionally make one live provider call. Costs money; never implied.",
    )
    args = parser.parse_args()

    report = Report()
    try:
        resolved = resolve_secret_files()
        report.add(
            "secrets.files",
            OK,
            f"{len(resolved)} secret(s) resolved from mounted files"
            if resolved
            else "none mounted",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("secrets.files", FAIL, str(exc))

    settings = check_configuration(report)
    if settings is not None:
        check_identity(report, settings)
        check_secrets(report, settings)
        check_network(report, settings)
        check_limits(report, settings)
        check_upload_limits(report, settings)
        check_worker_bounds(report, settings)
        check_models(report, settings)
        check_safety_invariants(report, settings)
        if args.check_provider:
            check_provider_reachable(report, settings)

    print(report.render())
    print()
    summary = {
        "passed": len([c for c in report.checks if c["status"] == OK]),
        "warnings": len(report.warned),
        "failures": len(report.failed),
    }
    print(
        f"{summary['passed']} passed, {summary['warnings']} warning(s), "
        f"{summary['failures']} failure(s)"
    )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"summary": summary, "checks": report.checks}, indent=2), encoding="utf-8"
        )

    if settings is None:
        raise SystemExit(2)
    raise SystemExit(1 if report.failed else 0)


if __name__ == "__main__":
    main()
