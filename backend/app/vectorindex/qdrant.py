"""Qdrant implementation of the vector-index protocol.

The only module that imports qdrant_client.

Consistency model, stated plainly: Qdrant gives per-operation acknowledgement, not multi-operation
transactions. A batch of upserts is therefore *not* atomic across batches, and this code never
pretends otherwise. Correctness comes from elsewhere:

* point identity is deterministic, so a replayed batch overwrites rather than duplicates;
* PostgreSQL holds the authoritative active-index pointer, and only a run that has been read back
  and reconciled is ever marked active;
* a replacement that fails leaves the previous active run untouched, because activation is a
  PostgreSQL state change that happens after verification, not a side effect of writing points.
"""

from typing import Any
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.http import models as rest

from app.embeddings.errors import EmbeddingError
from app.retrieval.errors import RetrievalError
from app.vectorindex.model import ScoredPoint, StoredPoint, VectorPoint, VectorSchema

DISTANCES = {
    "DOT": rest.Distance.DOT,
    "COSINE": rest.Distance.COSINE,
    "EUCLID": rest.Distance.EUCLID,
}


def _filter(
    selector: dict[str, Any], any_selector: dict[str, tuple[str, ...]] | None = None
) -> rest.Filter | None:
    """Conjunction of exact-match and any-of conditions, all evaluated inside Qdrant.

    Filtering server-side is what makes tenant isolation real: the alternative — searching the
    whole collection and discarding foreign rows afterwards — would already have read another
    tenant's points into this process and would silently lose result slots to them.
    """
    conditions: list[Any] = [
        rest.FieldCondition(key=key, match=rest.MatchValue(value=str(value)))
        for key, value in sorted(selector.items())
    ]
    for key, values in sorted((any_selector or {}).items()):
        conditions.append(
            rest.FieldCondition(key=key, match=rest.MatchAny(any=[str(value) for value in values]))
        )
    return rest.Filter(must=conditions) if conditions else None


class QdrantVectorIndex:
    def __init__(self, url: str, timeout: int = 60, retries: int = 3) -> None:
        self.url, self.timeout, self.retries = url, timeout, max(0, retries)
        self._client: QdrantClient | None = None

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            try:
                self._client = QdrantClient(url=self.url, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 - vendor errors map to one safe code
                raise EmbeddingError("VECTOR_INDEX_UNAVAILABLE") from exc
        return self._client

    def _call(self, operation: str, action: Any) -> Any:
        """Run one index call, mapping vendor failures to declared, retryable domain errors."""
        codes = {
            "upsert": "VECTOR_UPSERT_FAILED",
            "alias": "VECTOR_ACTIVATION_FAILED",
        }
        last: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                return action()
            except Exception as exc:  # noqa: BLE001 - vendor exception hierarchy is broad
                last = exc
        raise EmbeddingError(codes.get(operation, "VECTOR_INDEX_UNAVAILABLE")) from last

    # ------------------------------------------------------------------ schema

    def healthy(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception:  # noqa: BLE001 - a probe reports false rather than raising
            return False

    def ensure_schema(self, schema: VectorSchema) -> bool:
        distance = DISTANCES.get(schema.distance)
        if distance is None:
            raise EmbeddingError("VECTOR_SCHEMA_MISMATCH", {"distance": schema.distance})
        exists = self._call("get", lambda: self.client.collection_exists(schema.collection))
        if exists:
            self.verify_schema(schema)
            return False
        self._call(
            "create",
            lambda: self.client.create_collection(
                collection_name=schema.collection,
                # Named from the start: BM25 sparse and late-interaction vectors are added to the
                # same points in later milestones without changing point identity.
                vectors_config={
                    schema.vector_name: rest.VectorParams(size=schema.dimension, distance=distance)
                },
            ),
        )
        for field in schema.payload_indexes:
            # Tenant-oriented indexing lets Qdrant group a tenant's points on disk, and every
            # retrieval query filters this key server-side.
            parameters: Any = (
                rest.KeywordIndexParams(type=rest.KeywordIndexType.KEYWORD, is_tenant=True)
                if field == schema.tenant_key
                else rest.PayloadSchemaType.KEYWORD
            )
            self._call(
                "index",
                lambda name=field, params=parameters: self.client.create_payload_index(
                    collection_name=schema.collection, field_name=name, field_schema=params
                ),
            )
        return True

    def verify_schema(self, schema: VectorSchema) -> None:
        info = self._call("get", lambda: self.client.get_collection(schema.collection))
        vectors = info.config.params.vectors
        if not isinstance(vectors, dict) or schema.vector_name not in vectors:
            raise EmbeddingError(
                "VECTOR_SCHEMA_MISMATCH",
                {"expected_vector": schema.vector_name, "collection": schema.collection},
            )
        params = vectors[schema.vector_name]
        if int(params.size) != schema.dimension:
            raise EmbeddingError(
                "VECTOR_SCHEMA_MISMATCH",
                {"expected_dimension": schema.dimension, "found": int(params.size)},
            )
        if str(params.distance).upper().endswith(schema.distance):
            return
        raise EmbeddingError(
            "VECTOR_SCHEMA_MISMATCH",
            {"expected_distance": schema.distance, "found": str(params.distance)},
        )

    # ------------------------------------------------------------------ points

    def upsert(self, schema: VectorSchema, points: tuple[VectorPoint, ...]) -> int:
        if not points:
            return 0
        structs = [
            rest.PointStruct(
                id=str(point.point_id),
                vector={schema.vector_name: list(point.vector)},
                payload=point.payload,
            )
            for point in points
        ]
        self._call(
            "upsert",
            lambda: self.client.upsert(
                collection_name=schema.collection, points=structs, wait=True
            ),
        )
        return len(structs)

    def count(self, schema: VectorSchema, selector: dict[str, Any]) -> int:
        result = self._call(
            "count",
            lambda: self.client.count(
                collection_name=schema.collection, count_filter=_filter(selector), exact=True
            ),
        )
        return int(result.count)

    def retrieve(
        self, schema: VectorSchema, point_ids: tuple[UUID, ...], with_vectors: bool = True
    ) -> tuple[StoredPoint, ...]:
        if not point_ids:
            return ()
        records = self._call(
            "retrieve",
            lambda: self.client.retrieve(
                collection_name=schema.collection,
                ids=[str(value) for value in point_ids],
                with_payload=True,
                with_vectors=with_vectors,
            ),
        )
        result = []
        for record in records:
            vector = None
            if with_vectors and isinstance(record.vector, dict):
                raw = record.vector.get(schema.vector_name)
                if raw is not None:
                    vector = tuple(float(number) for number in raw)
            result.append(
                StoredPoint(
                    point_id=UUID(str(record.id)),
                    vector=vector,
                    payload=dict(record.payload or {}),
                )
            )
        return tuple(result)

    def scroll_ids(self, schema: VectorSchema, selector: dict[str, Any]) -> tuple[UUID, ...]:
        found: list[UUID] = []
        offset: Any = None
        while True:
            records, offset = self._call(
                "scroll",
                lambda cursor=offset: self.client.scroll(
                    collection_name=schema.collection,
                    scroll_filter=_filter(selector),
                    limit=512,
                    offset=cursor,
                    with_payload=False,
                    with_vectors=False,
                ),
            )
            found.extend(UUID(str(record.id)) for record in records)
            if offset is None:
                return tuple(found)

    def search(
        self,
        schema: VectorSchema,
        vector: tuple[float, ...],
        *,
        limit: int,
        selector: dict[str, Any],
        any_selector: dict[str, tuple[str, ...]] | None = None,
    ) -> tuple[ScoredPoint, ...]:
        if len(vector) != schema.dimension:
            raise RetrievalError(
                "RETRIEVAL_VECTOR_SPACE_MISMATCH",
                {"expected_dimension": schema.dimension, "query_dimension": len(vector)},
            )
        condition = _filter(selector, any_selector)
        if condition is None:
            # Refused rather than defaulted: an unscoped search is never what retrieval wants.
            raise RetrievalError("DENSE_SEARCH_FAILED", {"reason": "refusing unfiltered search"})
        try:
            result = self.client.query_points(
                collection_name=schema.collection,
                query=list(vector),
                using=schema.vector_name,
                limit=limit,
                query_filter=condition,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:  # noqa: BLE001 - vendor exception hierarchy is broad
            raise RetrievalError("DENSE_SEARCH_FAILED") from exc
        return tuple(
            ScoredPoint(
                point_id=UUID(str(point.id)),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in result.points
        )

    def delete(self, schema: VectorSchema, selector: dict[str, Any]) -> int:
        condition = _filter(selector)
        if condition is None:
            raise EmbeddingError("VECTOR_SCHEMA_MISMATCH", {"reason": "refusing unfiltered delete"})
        before = self.count(schema, selector)
        self._call(
            "delete",
            lambda: self.client.delete(
                collection_name=schema.collection,
                points_selector=rest.FilterSelector(filter=condition),
                wait=True,
            ),
        )
        return before

    # ------------------------------------------------------------------ aliases

    def set_alias(self, alias: str, collection: str) -> None:
        """Point the read alias at a collection. Qdrant applies an alias change atomically."""
        self._call(
            "alias",
            lambda: self.client.update_collection_aliases(
                change_aliases_operations=[
                    rest.CreateAliasOperation(
                        create_alias=rest.CreateAlias(collection_name=collection, alias_name=alias)
                    )
                ]
            ),
        )

    def alias_target(self, alias: str) -> str | None:
        aliases = self._call("get", lambda: self.client.get_aliases())
        for record in aliases.aliases:
            if record.alias_name == alias:
                return str(record.collection_name)
        return None
