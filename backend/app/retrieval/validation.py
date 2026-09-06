"""Lexical index reconciliation.

Everything measured here is structural: does the built index cover exactly the chunks it claims
to, do its aggregates agree with its own postings, and is every row inside the tenant and corpus
version it was built for. None of it says anything about whether BM25 *ranks* well, and none of it
is a retrieval or medical accuracy measure. Retrieval quality is measured separately, against a
gold query set, and is reported as its own numbers.

The philosophy is M4's: a durable expected set, an independent read-back, and activation only
after the two agree.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.retrieval_config import SparseIndexConfig
from app.models.chunking import Chunk
from app.models.embeddings import ChunkEmbedding, EmbeddingRun
from app.models.retrieval import SparseDocument, SparseIndex, SparsePosting, SparseTerm

SPARSE_CHUNK_MISSING = "SPARSE_CHUNK_MISSING"
SPARSE_CHUNK_UNEXPECTED = "SPARSE_CHUNK_UNEXPECTED"
SPARSE_COUNT_MISMATCH = "SPARSE_COUNT_MISMATCH"
SPARSE_TENANT_MISMATCH = "SPARSE_TENANT_MISMATCH"
SPARSE_CORPUS_VERSION_MISMATCH = "SPARSE_CORPUS_VERSION_MISMATCH"
SPARSE_TERM_STATISTICS_MISMATCH = "SPARSE_TERM_STATISTICS_MISMATCH"
SPARSE_ORPHAN_POSTING = "SPARSE_ORPHAN_POSTING"
SPARSE_CHUNK_TYPE_MISMATCH = "SPARSE_CHUNK_TYPE_MISMATCH"
SPARSE_EMPTY_DOCUMENT = "SPARSE_EMPTY_DOCUMENT"

MESSAGES = {
    SPARSE_CHUNK_MISSING: "An embedded chunk is absent from the lexical index.",
    SPARSE_CHUNK_UNEXPECTED: "The lexical index holds a chunk the dense lane did not embed.",
    SPARSE_COUNT_MISMATCH: "The indexed chunk count does not match the expected count.",
    SPARSE_TENANT_MISMATCH: "A lexical index row carries the wrong tenant.",
    SPARSE_CORPUS_VERSION_MISMATCH: "A lexical index row belongs to another chunk dataset.",
    SPARSE_TERM_STATISTICS_MISMATCH: "Term statistics disagree with the stored postings.",
    SPARSE_ORPHAN_POSTING: "A posting refers to a chunk the index does not contain.",
    SPARSE_CHUNK_TYPE_MISMATCH: "A lexical index row records the wrong chunk type.",
    SPARSE_EMPTY_DOCUMENT: "A chunk produced no lexical terms and can never be matched.",
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


@dataclass(frozen=True, slots=True)
class SparseReconciliation:
    findings: tuple[Finding, ...]
    verified: int
    expected: int
    corpus_fingerprint: str

    @property
    def ok(self) -> bool:
        return not [item for item in self.findings if item.severity in {"CRITICAL", "ERROR"}]


def reconcile_sparse(
    session: Session, index: SparseIndex, config: SparseIndexConfig
) -> SparseReconciliation:
    """Compare the built lexical index against the dense lane's recorded chunk set."""
    findings: list[Finding] = []

    embedding_run = session.scalar(
        select(EmbeddingRun).where(
            EmbeddingRun.document_version_id == index.document_version_id,
            EmbeddingRun.tenant_id == index.tenant_id,
            EmbeddingRun.chunk_run_id == index.chunk_run_id,
            EmbeddingRun.is_active.is_(True),
            EmbeddingRun.status == "SUCCEEDED",
        )
    )
    expected: set[UUID] = set()
    if embedding_run is not None:
        expected = set(
            session.scalars(
                select(ChunkEmbedding.chunk_id).where(
                    ChunkEmbedding.embedding_run_id == embedding_run.id
                )
            )
        )

    rows = session.execute(
        select(
            SparseDocument.chunk_id,
            SparseDocument.tenant_id,
            SparseDocument.chunk_run_id,
            SparseDocument.chunk_type,
            SparseDocument.length,
            SparseDocument.distinct_terms,
        ).where(SparseDocument.sparse_index_id == index.id)
    ).all()
    observed = {row.chunk_id: row for row in rows}

    for absent in sorted(expected - set(observed), key=str):
        findings.append(_finding(SPARSE_CHUNK_MISSING, chunk_id=absent))
    for extra in sorted(set(observed) - expected, key=str):
        findings.append(_finding(SPARSE_CHUNK_UNEXPECTED, chunk_id=extra))

    types: dict[UUID, str] = {
        chunk_id: chunk_type
        for chunk_id, chunk_type in session.execute(
            select(Chunk.id, Chunk.chunk_type).where(Chunk.chunk_run_id == index.chunk_run_id)
        ).all()
    }
    verified = 0
    for chunk_id, row in observed.items():
        problems = 0
        if row.tenant_id != index.tenant_id:
            findings.append(_finding(SPARSE_TENANT_MISMATCH, chunk_id=chunk_id))
            problems += 1
        if row.chunk_run_id != index.chunk_run_id:
            findings.append(_finding(SPARSE_CORPUS_VERSION_MISMATCH, chunk_id=chunk_id))
            problems += 1
        if types.get(chunk_id) is not None and types[chunk_id] != row.chunk_type:
            findings.append(
                _finding(
                    SPARSE_CHUNK_TYPE_MISMATCH,
                    chunk_id=chunk_id,
                    recorded=row.chunk_type,
                    actual=types[chunk_id],
                )
            )
            problems += 1
        if row.length == 0 or row.distinct_terms == 0:
            # Not fatal: a chunk can legitimately be a bare figure reference with no lexical
            # content. It is reported because such a chunk is unreachable through this lane and
            # depends entirely on the dense lane to ever be retrieved.
            findings.append(_finding(SPARSE_EMPTY_DOCUMENT, severity="WARNING", chunk_id=chunk_id))
        if not problems:
            verified += 1

    if len(observed) != len(expected):
        findings.append(
            _finding(SPARSE_COUNT_MISMATCH, expected=len(expected), indexed=len(observed))
        )

    if config.verify_postings:
        orphans = session.scalar(
            select(func.count())
            .select_from(SparsePosting)
            .where(
                SparsePosting.sparse_index_id == index.id,
                SparsePosting.chunk_id.not_in(
                    select(SparseDocument.chunk_id).where(
                        SparseDocument.sparse_index_id == index.id
                    )
                ),
            )
        )
        if orphans:
            findings.append(_finding(SPARSE_ORPHAN_POSTING, postings=int(orphans)))

        # The term table is a derived aggregate of the postings, and query-time inverse document
        # frequency reads it instead of the postings. If the two ever disagreed, every score in
        # this index would be computed from statistics that describe a different corpus.
        recomputed = session.execute(
            select(
                SparsePosting.term,
                func.count(),
                func.coalesce(func.sum(SparsePosting.term_frequency), 0),
            )
            .where(SparsePosting.sparse_index_id == index.id)
            .group_by(SparsePosting.term)
        ).all()
        stored = {
            term: (document_frequency, total_frequency)
            for term, document_frequency, total_frequency in session.execute(
                select(
                    SparseTerm.term, SparseTerm.document_frequency, SparseTerm.total_frequency
                ).where(SparseTerm.sparse_index_id == index.id)
            ).all()
        }
        divergent = [
            term
            for term, document_frequency, total_frequency in recomputed
            if stored.get(term) != (int(document_frequency), int(total_frequency))
        ]
        if divergent or len(stored) != len(recomputed):
            findings.append(
                _finding(
                    SPARSE_TERM_STATISTICS_MISMATCH,
                    terms=divergent[:20],
                    divergent=len(divergent),
                    stored=len(stored),
                    recomputed=len(recomputed),
                )
            )

    digest = hashlib.sha256()
    for chunk_id in sorted(observed, key=str):
        row = observed[chunk_id]
        digest.update(f"{chunk_id}:{row.length}:{row.distinct_terms}\x1f".encode())
    return SparseReconciliation(
        findings=tuple(findings),
        verified=verified,
        expected=len(expected),
        corpus_fingerprint=digest.hexdigest(),
    )
