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
from sqlalchemy import Integer, func, select
from starlette.middleware.base import RequestResponseEndpoint

from app.api.chunking import router as chunking_router
from app.api.documents import router
from app.api.embeddings import router as embedding_router
from app.api.parsing import router as parsing_router
from app.core.config import Settings
from app.core.errors import DomainError
from app.db.session import make_engine, make_sessions
from app.models.chunking import (
    Chunk,
    ChunkRun,
    ChunkValidationFinding,
    QuestionArtifact,
)
from app.models.documents import IngestionJob, IngestionStageEvent
from app.models.embeddings import (
    ChunkEmbedding,
    EmbeddingRun,
    IndexRun,
    IndexValidationFinding,
)
from app.models.enums import ParseRunStatus
from app.models.parsing import ParseRun, ParseValidationFinding
from app.observability.ingestion import IngestionMetrics
from app.services.control import ControlPlane
from app.services.health import DependencyProbe, InfrastructureProbe
from app.services.storage import S3ObjectStorage
from app.vectorindex.qdrant import QdrantVectorIndex

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


def _chunk_metric_lines(service: ControlPlane) -> list[str]:
    """Chunk counters scraped from durable PostgreSQL state, on the same basis as parsing.

    These describe how much structure was built and how it validated. None of them is a
    retrieval or medical accuracy measure; nothing is retrievable at this milestone.
    """
    with service.sessions() as session:
        runs = session.execute(
            select(ChunkRun.status, func.count()).group_by(ChunkRun.status)
        ).all()
        results = session.execute(
            select(ChunkRun.validation_result, func.count())
            .where(ChunkRun.validation_result.is_not(None))
            .group_by(ChunkRun.validation_result)
        ).all()
        findings = session.execute(
            select(ChunkValidationFinding.severity, func.count()).group_by(
                ChunkValidationFinding.severity
            )
        ).all()
        chunks = session.execute(
            select(Chunk.chunk_type, func.count()).group_by(Chunk.chunk_type)
        ).all()
        questions = session.scalar(select(func.count()).select_from(QuestionArtifact)) or 0
    lines = ["# TYPE chunk_runs_by_status gauge"]
    lines += [f'chunk_runs_by_status{{status="{status}"}} {count}' for status, count in runs]
    lines += ["# TYPE chunk_runs_by_result gauge"]
    lines += [f'chunk_runs_by_result{{result="{result}"}} {count}' for result, count in results]
    lines += ["# TYPE chunk_validation_findings gauge"]
    lines += [
        f'chunk_validation_findings{{severity="{severity}"}} {count}'
        for severity, count in findings
    ]
    lines += ["# TYPE chunks_by_type gauge"]
    lines += [f'chunks_by_type{{chunk_type="{kind}"}} {count}' for kind, count in chunks]
    lines += ["# TYPE question_artifacts_total gauge", f"question_artifacts_total {int(questions)}"]
    return lines


def _index_metric_lines(service: ControlPlane) -> list[str]:
    """Embedding and index counters from durable PostgreSQL state.

    Embedding runs in the Celery worker, which is not scraped, so the authoritative counts come
    from committed rows. Labels are bounded enum values only: no chunk, document, version or run
    identifier is ever a label. None of these is a retrieval or medical accuracy measure.
    """
    with service.sessions() as session:
        runs = session.execute(
            select(EmbeddingRun.status, func.count()).group_by(EmbeddingRun.status)
        ).all()
        index_runs = session.execute(
            select(IndexRun.status, func.count()).group_by(IndexRun.status)
        ).all()
        findings = session.execute(
            select(IndexValidationFinding.severity, func.count()).group_by(
                IndexValidationFinding.severity
            )
        ).all()
        embeddings, reused, tokens = session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(ChunkEmbedding.reused.cast(Integer)), 0),
                func.coalesce(func.sum(ChunkEmbedding.token_count), 0),
            )
        ).one()
        points = session.scalar(
            select(func.coalesce(func.sum(IndexRun.verified_point_count), 0)).where(
                IndexRun.is_active.is_(True)
            )
        )
    lines = ["# TYPE embedding_runs_by_status gauge"]
    lines += [f'embedding_runs_by_status{{status="{status}"}} {count}' for status, count in runs]
    lines += ["# TYPE index_runs_by_status gauge"]
    lines += [f'index_runs_by_status{{status="{status}"}} {count}' for status, count in index_runs]
    lines += ["# TYPE index_validation_findings gauge"]
    lines += [
        f'index_validation_findings{{severity="{severity}"}} {count}'
        for severity, count in findings
    ]
    for name, value in (
        ("embeddings_total", embeddings),
        ("embeddings_reused_total", reused),
        ("embedding_input_tokens_total", tokens),
        ("index_active_points_total", points or 0),
    ):
        lines += [f"# TYPE {name} gauge", f"{name} {int(value)}"]
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
        # The API reads live index statistics on an operator's behalf; it never embeds and never
        # writes points. Constructing the client is lazy inside the adapter, so an unreachable
        # Qdrant degrades the statistics endpoint rather than preventing startup.
        service = ControlPlane(
            config,
            make_sessions(engine),
            S3ObjectStorage(config),
            metrics,
            vector_index=QdrantVectorIndex(
                config.qdrant_url, timeout=config.index.request_timeout_seconds
            ),
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(title="Medical RAG - M4", version="0.4.0", lifespan=lifespan)
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
        return {"status": "alive", "milestone": "M4"}

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
            lines += _chunk_metric_lines(service)
            lines += _index_metric_lines(service)
            result += ("\n".join(lines) + "\n").encode()
        return Response(result, headers={"Content-Type": CONTENT_TYPE_LATEST})

    app.include_router(router)
    app.include_router(parsing_router)
    app.include_router(chunking_router)
    app.include_router(embedding_router)
    return app
