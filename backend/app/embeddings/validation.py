"""Vector sanity checks and index reconciliation.

Everything measured here is structural: shape, finiteness, identity and agreement between what
PostgreSQL says should exist and what the vector index actually holds. None of it says anything
about whether a vector is a *good* representation, and none of it is a retrieval or medical
accuracy measure. Retrieval quality begins in the next milestone, with queries and a gold set.
"""

import math
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.embedding_config import EmbeddingConfig
from app.embeddings.model import EmbeddingVector

# Vectors
EMBEDDING_DIMENSION_MISMATCH = "EMBEDDING_DIMENSION_MISMATCH"
EMBEDDING_NON_FINITE = "EMBEDDING_NON_FINITE"
EMBEDDING_ZERO_VECTOR = "EMBEDDING_ZERO_VECTOR"
EMBEDDING_CHECKSUM_MISMATCH = "EMBEDDING_CHECKSUM_MISMATCH"
EMBEDDING_INPUT_TOO_LONG = "EMBEDDING_INPUT_TOO_LONG"
# Index reconciliation
QDRANT_POINT_MISSING = "QDRANT_POINT_MISSING"
QDRANT_POINT_UNEXPECTED = "QDRANT_POINT_UNEXPECTED"
QDRANT_PAYLOAD_MISMATCH = "QDRANT_PAYLOAD_MISMATCH"
QDRANT_TENANT_MISMATCH = "QDRANT_TENANT_MISMATCH"
QDRANT_VECTOR_NAME_MISMATCH = "QDRANT_VECTOR_NAME_MISMATCH"
QDRANT_COUNT_MISMATCH = "QDRANT_COUNT_MISMATCH"
INDEX_RECONCILIATION_FAILED = "INDEX_RECONCILIATION_FAILED"

MESSAGES = {
    EMBEDDING_DIMENSION_MISMATCH: "A vector does not have the configured dimension.",
    EMBEDDING_NON_FINITE: "A vector contains NaN or infinite values.",
    EMBEDDING_ZERO_VECTOR: "A vector is entirely zero and carries no representation.",
    EMBEDDING_CHECKSUM_MISMATCH: "A stored vector does not match the recorded checksum.",
    EMBEDDING_INPUT_TOO_LONG: "A chunk exceeds the model input limit and was not embedded.",
    QDRANT_POINT_MISSING: "An expected point is absent from the vector index.",
    QDRANT_POINT_UNEXPECTED: "The vector index holds a point this run did not record.",
    QDRANT_PAYLOAD_MISMATCH: "A stored point payload does not match its recorded provenance.",
    QDRANT_TENANT_MISMATCH: "A stored point carries the wrong tenant.",
    QDRANT_VECTOR_NAME_MISMATCH: "A stored point has no vector under the configured name.",
    QDRANT_COUNT_MISMATCH: "The indexed point count does not match the expected count.",
    INDEX_RECONCILIATION_FAILED: "The index does not reconcile with the recorded embeddings.",
}


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    code: str
    message: str
    chunk_id: UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _finding(code: str, severity: str = "CRITICAL", **details: Any) -> Finding:
    chunk = details.pop("chunk_id", None)
    return Finding(
        severity=severity, code=code, message=MESSAGES[code], chunk_id=chunk, details=details
    )


def check_vector(vector: EmbeddingVector, config: EmbeddingConfig) -> list[Finding]:
    """Reject a vector that cannot be a valid representation, before it is ever stored."""
    findings: list[Finding] = []
    if len(vector.values) != config.embedding_dimension:
        findings.append(
            _finding(
                EMBEDDING_DIMENSION_MISMATCH,
                chunk_id=vector.chunk_id,
                expected=config.embedding_dimension,
                produced=len(vector.values),
            )
        )
    if any(not math.isfinite(value) for value in vector.values):
        findings.append(_finding(EMBEDDING_NON_FINITE, chunk_id=vector.chunk_id))
    elif not any(value != 0.0 for value in vector.values):
        findings.append(_finding(EMBEDDING_ZERO_VECTOR, chunk_id=vector.chunk_id))
    return findings


@dataclass(frozen=True, slots=True)
class ExpectedPoint:
    """What PostgreSQL says the index must hold for one chunk."""

    point_id: UUID
    chunk_id: UUID
    vector_checksum: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ObservedPoint:
    point_id: UUID
    vector: tuple[float, ...] | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    findings: tuple[Finding, ...]
    verified: int
    expected: int
    observed: int

    @property
    def ok(self) -> bool:
        return not self.findings and self.verified == self.expected == self.observed


# Payload keys whose exact agreement is what makes a retrieved point trustworthy provenance.
CRITICAL_PAYLOAD = (
    "tenant_id",
    "document_id",
    "document_version_id",
    "chunk_run_id",
    "embedding_run_id",
    "chunk_id",
    "chunk_type",
)


def reconcile(
    expected: tuple[ExpectedPoint, ...],
    observed: tuple[ObservedPoint, ...],
    index_ids: tuple[UUID, ...],
    config: EmbeddingConfig,
    checksum: Any,
    verify_vectors: bool = True,
) -> Reconciliation:
    """Compare the recorded embeddings against what the index actually returned.

    `checksum` is the same canonical vector-checksum function used when the vector was produced,
    so a byte-level difference between what was computed and what came back is detected rather
    than assumed away.
    """
    findings: list[Finding] = []
    by_id = {point.point_id: point for point in observed}
    wanted = {point.point_id for point in expected}

    for extra in sorted(set(index_ids) - wanted, key=str):
        findings.append(_finding(QDRANT_POINT_UNEXPECTED, point_id=str(extra)))
    missing_ids = wanted - set(index_ids)
    for absent in sorted(missing_ids, key=str):
        findings.append(_finding(QDRANT_POINT_MISSING, point_id=str(absent)))

    verified = 0
    for point in expected:
        stored = by_id.get(point.point_id)
        if stored is None:
            if point.point_id not in missing_ids:
                findings.append(
                    _finding(
                        QDRANT_POINT_MISSING,
                        chunk_id=point.chunk_id,
                        point_id=str(point.point_id),
                    )
                )
            continue
        problems = 0
        if verify_vectors:
            if stored.vector is None:
                findings.append(_finding(QDRANT_VECTOR_NAME_MISMATCH, chunk_id=point.chunk_id))
                problems += 1
            else:
                if len(stored.vector) != config.embedding_dimension:
                    findings.append(
                        _finding(
                            EMBEDDING_DIMENSION_MISMATCH,
                            chunk_id=point.chunk_id,
                            expected=config.embedding_dimension,
                            produced=len(stored.vector),
                        )
                    )
                    problems += 1
                elif checksum(stored.vector) != point.vector_checksum:
                    findings.append(_finding(EMBEDDING_CHECKSUM_MISMATCH, chunk_id=point.chunk_id))
                    problems += 1
        if str(stored.payload.get("tenant_id")) != str(point.payload.get("tenant_id")):
            findings.append(_finding(QDRANT_TENANT_MISMATCH, chunk_id=point.chunk_id))
            problems += 1
        divergent = [
            key
            for key in CRITICAL_PAYLOAD
            if key != "tenant_id" and str(stored.payload.get(key)) != str(point.payload.get(key))
        ]
        if divergent:
            findings.append(
                _finding(QDRANT_PAYLOAD_MISMATCH, chunk_id=point.chunk_id, keys=divergent)
            )
            problems += 1
        if not problems:
            verified += 1

    if len(index_ids) != len(expected):
        findings.append(
            _finding(QDRANT_COUNT_MISMATCH, expected=len(expected), indexed=len(index_ids))
        )
    return Reconciliation(
        findings=tuple(findings),
        verified=verified,
        expected=len(expected),
        observed=len(index_ids),
    )


def result(findings: tuple[Finding, ...]) -> str:
    severities = {finding.severity for finding in findings}
    if "CRITICAL" in severities:
        return "FAIL"
    if "ERROR" in severities:
        return "NEEDS_REVIEW"
    return "PASS_WITH_WARNINGS" if "WARNING" in severities else "PASS"
