"""The retrieval service: a query encoder behind a small internal HTTP boundary.

This process exists so the MedCPT **query** encoder is loaded exactly once, in one place, instead
of into every API worker and alongside the ingestion worker's warm *article* encoder. The two
workloads scale differently: embedding is a long batch job over a whole corpus, query encoding is
a short interactive call, and keeping them in separate processes lets either be restarted, sized
or moved without disturbing the other.

Its responsibility is deliberately tiny — text in, vector out. It holds no database connection,
no tenant concept, no authorization and no document access, so it cannot become a second place
where access decisions are made. It is internal infrastructure on the same footing as Qdrant: it
is not published, the browser never reaches it, and it never logs question text.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.errors import DomainError
from app.retrieval.model import QueryEncoderSpec
from app.retrieval.query.medcpt import MedCPTQueryEncoder, configure_offline_environment

logger = logging.getLogger("medical_rag.retrieval")


class EncodeRequest(BaseModel):
    # An empty list is legitimate: it asks only for the loaded encoder's specification, which is
    # how the API verifies the pinned revision before it trusts a vector.
    queries: list[str] = Field(default_factory=list, max_length=64)


def _specification(spec: QueryEncoderSpec) -> dict[str, Any]:
    return {
        "provider": spec.provider,
        "model_id": spec.model_id,
        "model_revision": spec.model_revision,
        "tokenizer_revision": spec.tokenizer_revision,
        "model_checksum": spec.model_checksum,
        "tokenizer_checksum": spec.tokenizer_checksum,
        "dimension": spec.dimension,
        "pooling": spec.pooling,
        "normalization": spec.normalization,
        "distance_metric": spec.distance_metric,
        "max_query_tokens": spec.max_query_tokens,
        "dtype": spec.dtype,
        "device": spec.device,
        "normalization_version": spec.normalization_version,
        "semantics_fingerprint": spec.semantics_fingerprint,
        "library_versions": spec.library_versions,
    }


def create_app(settings: Settings | None = None, encoder: Any = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    policy = config.query_encoder
    state: dict[str, Any] = {"encoder": encoder}

    def loaded() -> Any:
        """Weights stay warm for the life of the process and load on first use, never on import."""
        if state["encoder"] is None:
            configure_offline_environment(policy)
            built = MedCPTQueryEncoder(policy)
            built.load()
            state["encoder"] = built
        return state["encoder"]

    app = FastAPI(title="Medical RAG - retrieval service", version="0.5.0")

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        # The declared code survives the hop so the API can report the real cause; the detail
        # never contains the question.
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive", "role": "query-encoder"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        try:
            specification = loaded().specification
        except DomainError as exc:
            return JSONResponse({"status": "not_ready", "code": exc.code}, status_code=503)
        return JSONResponse(
            {"status": "ready", "model_revision": specification.model_revision}, status_code=200
        )

    @app.post("/measure")
    def measure(body: EncodeRequest) -> dict[str, Any]:
        encoder_instance = loaded()
        measurements = encoder_instance.measure(body.queries)
        return {
            "specification": _specification(encoder_instance.specification),
            "measurements": [
                {"token_count": item.token_count, "exceeds_limit": item.exceeds_limit}
                for item in measurements
            ],
        }

    @app.post("/encode")
    def encode(body: EncodeRequest) -> dict[str, Any]:
        encoder_instance = loaded()
        vectors = encoder_instance.encode_queries(body.queries) if body.queries else ()
        # Only counts and hashes are logged. The question itself never reaches a log line here.
        logger.info(
            "query_encoded",
            extra={"event": "query_encoded", "count": len(vectors)},
        )
        return {
            "specification": _specification(encoder_instance.specification),
            "vectors": [
                {
                    "values": list(vector.values),
                    "token_count": vector.token_count,
                    "normalized": vector.normalized,
                    "query_hash": vector.query_hash,
                    "checksum": vector.checksum,
                    "norm": vector.norm,
                    "cached": vector.cached,
                }
                for vector in vectors
            ],
        }

    return app
