"""Provider-independent vector-index contracts.

Only `qdrant.py` imports qdrant_client. Services depend on this protocol, so the index engine is
one module of work to replace and no orchestration code can come to depend on Qdrant specifics.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class VectorSchema:
    """The physical shape one collection must have for the configured representation."""

    collection: str
    vector_name: str
    dimension: int
    distance: str
    payload_indexes: tuple[str, ...]
    tenant_key: str


@dataclass(frozen=True, slots=True)
class VectorPoint:
    """One retrieval-eligible chunk as it is stored in the index.

    The payload deliberately carries routing, filtering and provenance only. Chunk text stays in
    PostgreSQL: duplicating source bodies into the index would spread licensed medical content
    across a second store for no retrieval benefit, since M5 resolves chunk ids anyway.
    """

    point_id: UUID
    chunk_id: UUID
    vector: tuple[float, ...]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredPoint:
    point_id: UUID
    vector: tuple[float, ...] | None
    payload: dict[str, Any] = field(default_factory=dict)


class VectorIndex(Protocol):
    def ensure_schema(self, schema: VectorSchema) -> bool:
        """Create the collection and payload indexes if absent. True when newly created.

        Must fail rather than adapt if an existing collection disagrees with the schema: silently
        writing 768-dimension vectors into a collection built for something else corrupts an index
        that other runs may already depend on.
        """
        ...

    def upsert(self, schema: VectorSchema, points: tuple[VectorPoint, ...]) -> int: ...

    def count(self, schema: VectorSchema, selector: dict[str, Any]) -> int: ...

    def retrieve(
        self, schema: VectorSchema, point_ids: tuple[UUID, ...], with_vectors: bool = True
    ) -> tuple[StoredPoint, ...]: ...

    def scroll_ids(self, schema: VectorSchema, selector: dict[str, Any]) -> tuple[UUID, ...]: ...

    def delete(self, schema: VectorSchema, selector: dict[str, Any]) -> int: ...

    def set_alias(self, alias: str, collection: str) -> None: ...

    def alias_target(self, alias: str) -> str | None: ...

    def healthy(self) -> bool: ...
