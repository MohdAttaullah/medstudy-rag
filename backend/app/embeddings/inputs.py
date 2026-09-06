"""Deterministic embedding-input construction and retrieval eligibility.

Two rules govern this module.

*Nothing is invented.* The context field is assembled from the chunk's own persisted hierarchy and
caption metadata; the body is the M3 retrieval representation exactly as stored. No model writes a
summary, and no text is paraphrased, expanded or "improved".

*The embedding input is not source content.* It is a derived representation used to place a chunk
in the vector space. The authoritative chain stays
original PDF -> parsed element -> chunk source text -> retrieval representation -> embedding input,
and `ChunkEmbedding.input_hash` records exactly which representation produced a given vector.
"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from app.core.embedding_config import EmbeddingConfig
from app.embeddings.model import EmbeddingInput


@dataclass(frozen=True, slots=True)
class ChunkSource:
    """The persisted M3 facts an embedding input may draw on. Nothing else is permitted."""

    chunk_id: UUID
    chunk_type: str
    retrieval_text: str
    document_title: str
    hierarchy: tuple[str, ...] = ()
    caption: str | None = None


def eligible(chunk_type: str, config: EmbeddingConfig) -> bool:
    """Is this chunk a first-stage retrieval unit?

    TEXT_PARENT is excluded by policy: a parent is the union of its children, so indexing it
    alongside them would return a whole section where a precise passage was wanted, and would
    double-count the same source text in the ranking.
    """
    if chunk_type == "TEXT_PARENT":
        return config.embed_parent_chunks
    return chunk_type in config.eligible_chunk_types


def _context(source: ChunkSource, config: EmbeddingConfig) -> str:
    """Compact provenance context: document, declared hierarchy, then any artifact caption."""
    parts: list[str] = []
    for value in (source.document_title, *source.hierarchy, source.caption or ""):
        text = " ".join(value.split())
        if text and text not in parts:
            parts.append(text)
    context = " > ".join(parts)
    if len(context) > config.max_context_characters:
        # Trim from the front: the nearest heading and the caption locate the passage far better
        # than the book title does. The trim is deterministic and is inside the input hash.
        context = "..." + context[-(config.max_context_characters - 3) :]
    return context


def build(source: ChunkSource, config: EmbeddingConfig) -> EmbeddingInput:
    context = _context(source, config)
    body = source.retrieval_text
    payload = {
        "builder": config.input_builder_version,
        "model": config.model_id,
        "revision": config.model_revision,
        "separator": config.context_separator,
        "context": context,
        "body": body,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EmbeddingInput(chunk_id=source.chunk_id, context=context, body=body, input_hash=digest)


def encoded(value: EmbeddingInput, config: EmbeddingConfig) -> tuple[str, str]:
    """The exact two fields handed to the encoder, in order.

    MedCPT's article representation is a title/abstract pair, so the compact context takes the
    first field and the retrieval representation takes the second. An empty context is passed as
    an empty string rather than being folded into the body, so the field boundary never moves.
    """
    _ = config
    return value.context, value.body
