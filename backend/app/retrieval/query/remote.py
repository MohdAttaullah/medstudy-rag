"""Query encoder reached over the internal network.

Interactive query encoding runs in its own process. The reason is concrete rather than
architectural taste: the API and the outbox dispatcher are built from the lean backend image and
carry no torch at all, and the ingestion worker already holds warm *article* encoder weights it
would never use for a question. Loading a second transformer into every web worker would add
hundreds of megabytes per process to serve a request that spends most of its time in PostgreSQL.

This client therefore speaks to the retrieval service, which owns exactly one responsibility:
turning text into a query vector. Authorization, tenant scoping, corpus resolution, filtering and
hydration all stay in the API next to the authenticated principal — the encoder never sees a
tenant, a user or a document, and is not a policy decision point.

The transport carries the question text to an internal service on a private network. It is not
published, not reachable from the browser, and does not log query text.
"""

from collections.abc import Sequence
from typing import Any

import httpx

from app.core.retrieval_config import QueryEncoderConfig
from app.retrieval.errors import MESSAGES, RetrievalError
from app.retrieval.model import QueryEncoderSpec, QueryMeasurement, QueryVector


def _spec(payload: dict[str, Any]) -> QueryEncoderSpec:
    return QueryEncoderSpec(
        provider=str(payload["provider"]),
        model_id=str(payload["model_id"]),
        model_revision=str(payload["model_revision"]),
        tokenizer_revision=str(payload["tokenizer_revision"]),
        model_checksum=str(payload["model_checksum"]),
        tokenizer_checksum=str(payload["tokenizer_checksum"]),
        dimension=int(payload["dimension"]),
        pooling=str(payload["pooling"]),
        normalization=str(payload["normalization"]),
        distance_metric=str(payload["distance_metric"]),
        max_query_tokens=int(payload["max_query_tokens"]),
        dtype=str(payload["dtype"]),
        device=str(payload["device"]),
        normalization_version=str(payload["normalization_version"]),
        semantics_fingerprint=str(payload["semantics_fingerprint"]),
        library_versions=dict(payload.get("library_versions") or {}),
    )


class HttpQueryEncoder:
    """`QueryEncoder` over HTTP. Declared failures survive the hop; nothing is substituted."""

    def __init__(self, config: QueryEncoderConfig) -> None:
        if not config.endpoint:
            raise RetrievalError("QUERY_ENCODER_UNAVAILABLE", {"reason": "no endpoint configured"})
        self.config = config
        self.endpoint = config.endpoint.rstrip("/")
        self._spec: QueryEncoderSpec | None = None

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.endpoint}{path}", json=body, timeout=self.config.request_timeout_seconds
            )
        except httpx.HTTPError as exc:
            raise RetrievalError("QUERY_ENCODER_UNAVAILABLE") from exc
        if response.status_code >= 400:
            code = ""
            try:
                code = str((response.json().get("error") or {}).get("code") or "")
            except Exception:  # noqa: BLE001 - a malformed error body is still a failure
                code = ""
            # A declared code is re-raised as itself so the caller sees the real cause; anything
            # else becomes "unavailable" rather than leaking a remote body to the client.
            raise RetrievalError(code if code in MESSAGES else "QUERY_ENCODER_UNAVAILABLE")
        result: dict[str, Any] = response.json()
        return result

    @property
    def specification(self) -> QueryEncoderSpec:
        if self._spec is None:
            self._spec = _spec(self._post("/encode", {"queries": []})["specification"])
        return self._spec

    def measure(self, queries: Sequence[str]) -> tuple[QueryMeasurement, ...]:
        payload = self._post("/measure", {"queries": list(queries)})
        return tuple(
            QueryMeasurement(
                token_count=int(item["token_count"]), exceeds_limit=bool(item["exceeds_limit"])
            )
            for item in payload["measurements"]
        )

    def encode_queries(self, queries: Sequence[str]) -> tuple[QueryVector, ...]:
        if not queries:
            return ()
        payload = self._post("/encode", {"queries": list(queries)})
        self._spec = _spec(payload["specification"])
        return tuple(
            QueryVector(
                values=tuple(float(number) for number in item["values"]),
                token_count=int(item["token_count"]),
                normalized=str(item["normalized"]),
                query_hash=str(item["query_hash"]),
                checksum=str(item["checksum"]),
                norm=float(item["norm"]),
                cached=bool(item.get("cached", False)),
            )
            for item in payload["vectors"]
        )
