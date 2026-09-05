import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, generate_latest
from sqlalchemy import func, select
from starlette.middleware.base import RequestResponseEndpoint

from app.api.documents import router
from app.api.parsing import router as parsing_router
from app.core.config import Settings
from app.core.errors import DomainError
from app.db.session import make_engine, make_sessions
from app.models.documents import IngestionJob, IngestionStageEvent
from app.models.enums import ParseRunStatus
from app.models.parsing import ParseRun, ParseValidationFinding
from app.observability.ingestion import IngestionMetrics
from app.services.control import ControlPlane
from app.services.health import DependencyProbe, InfrastructureProbe
from app.services.storage import S3ObjectStorage

logger = logging.getLogger("medical_rag.requests")


def _parse_metric_lines(service: ControlPlane) -> list[str]:
    """Parse counters scraped from durable PostgreSQL state.

    Parsing runs in the Celery worker, which is not scraped directly, so the authoritative
    counts come from the rows the worker committed rather than from in-process counters.
    Labels are bounded enum values only; no document, version or run identifier is a label.
    """
    with service.sessions() as session:
        runs = session.execute(
            select(ParseRun.status, func.count()).group_by(ParseRun.status)
        ).all()
        results = session.execute(
            select(ParseRun.validation_result, func.count())
            .where(ParseRun.validation_result.is_not(None))
            .group_by(ParseRun.validation_result)
        ).all()
        findings = session.execute(
            select(ParseValidationFinding.severity, func.count()).group_by(
                ParseValidationFinding.severity
            )
        ).all()
        totals = session.execute(
            select(
                func.coalesce(func.sum(ParseRun.page_count), 0),
                func.coalesce(func.sum(ParseRun.table_count), 0),
                func.coalesce(func.sum(ParseRun.figure_count), 0),
                func.coalesce(func.sum(ParseRun.formula_count), 0),
                func.coalesce(func.sum(ParseRun.ocr_page_count), 0),
            ).where(ParseRun.status == ParseRunStatus.SUCCEEDED)
        ).one()
    lines = ["# TYPE parse_runs_by_status gauge"]
    lines += [f'parse_runs_by_status{{status="{status.value}"}} {count}' for status, count in runs]
    lines += ["# TYPE parse_runs_by_result gauge"]
    lines += [
        f'parse_runs_by_result{{result="{result.value}"}} {count}' for result, count in results
    ]
    lines += ["# TYPE parse_validation_findings gauge"]
    lines += [
        f'parse_validation_findings{{severity="{severity.value}"}} {count}'
        for severity, count in findings
    ]
    names = ("pages", "tables", "figures", "formulas", "ocr_pages")
    for name, value in zip(names, totals, strict=True):
        lines += [f"# TYPE parse_{name}_total gauge", f"parse_{name}_total {int(value)}"]
    return lines


def create_app(
    settings: Settings | None = None,
    probe: DependencyProbe | None = None,
    control_plane: ControlPlane | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings()
    dependencies = probe if probe is not None else InfrastructureProbe(config)
    registry = CollectorRegistry()
    metrics = IngestionMetrics(registry)
    engine = None
    service = control_plane
    if service is None and config.database_url.get_secret_value():
        engine = make_engine(config)
        service = ControlPlane(config, make_sessions(engine), S3ObjectStorage(config), metrics)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(title="Medical RAG - M2", version="0.3.0", lifespan=lifespan)
    app.state.settings, app.state.control = config, service
    requests = Counter(
        "medrag_http_requests_total", "Completed HTTP requests", ["status"], registry=registry
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "Idempotency-Key",
            "X-Upload-Metadata",
        ],
        expose_headers=["X-Request-ID"],
    )

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "correlation_id": request.state.request_id,
            },
            status_code=exc.status,
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never return Pydantic input values, raw header tokens or document metadata in errors.
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Check the request fields and pagination values.",
                    "details": {},
                },
                "correlation_id": request.state.request_id,
            },
            status_code=422,
        )

    @app.middleware("http")
    async def correlate(request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            request_id = str(UUID(request.headers.get("X-Request-ID", "")))
        except ValueError:
            request_id = str(uuid4())
        request.state.request_id = request_id
        start = perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "request_failed",
                extra={
                    "event": "request_failed",
                    "request_id": request_id,
                    "error_type": type(exc).__name__,
                },
            )
            response = JSONResponse(
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "The request could not be completed.",
                        "details": {},
                    },
                    "correlation_id": request_id,
                },
                status_code=500,
            )
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        requests.labels(status=str(response.status_code)).inc()
        logger.info(
            "request_complete",
            extra={
                "event": "request_complete",
                "request_id": request_id,
                "status": response.status_code,
                "duration_ms": (perf_counter() - start) * 1000,
            },
        )
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive", "milestone": "M2"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        checks = await dependencies.check()
        healthy = bool(checks) and all(checks.values())
        return JSONResponse(
            {"status": "ready" if healthy else "not_ready", "dependencies": checks},
            status_code=200 if healthy else 503,
        )

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        result = generate_latest(registry)
        # Job gauges and failure count derive from durable PostgreSQL history, including workers.
        if service is not None:
            with service.sessions() as session:
                states = session.execute(
                    select(IngestionJob.status, func.count()).group_by(IngestionJob.status)
                ).all()
                failures = (
                    session.scalar(
                        select(func.count())
                        .select_from(IngestionStageEvent)
                        .where(IngestionStageEvent.to_status == "FAILED")
                    )
                    or 0
                )
            lines = ["# TYPE ingestion_jobs_by_status gauge"] + [
                f'ingestion_jobs_by_status{{status="{state.value}"}} {count}'
                for state, count in states
            ]
            lines += [
                "# TYPE ingestion_jobs_failed_total counter",
                f"ingestion_jobs_failed_total {failures}",
            ]
            lines += _parse_metric_lines(service)
            result += ("\n".join(lines) + "\n").encode()
        return Response(result, headers={"Content-Type": CONTENT_TYPE_LATEST})

    app.include_router(router)
    app.include_router(parsing_router)
    return app
