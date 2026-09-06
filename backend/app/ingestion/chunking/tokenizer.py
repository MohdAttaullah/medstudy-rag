"""Offline WordPiece measurement; splitting slices source offsets, never decoded/lowercased text."""

import hashlib
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

from tokenizers import Tokenizer

from app.core.chunking_config import ChunkingConfig
from app.ingestion.chunking.errors import ChunkError


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...
    def offsets(self, text: str) -> list[tuple[int, int]]: ...


class LocalTokenizer:
    def __init__(self, config: ChunkingConfig, path: Path | None = None) -> None:
        path = path or Path(__file__).parent / "assets/medcpt/tokenizer.json"
        try:
            if version("tokenizers") != config.tokenizer_runtime:
                raise ValueError("Tokenizer runtime mismatch")
            if hashlib.sha256(path.read_bytes()).hexdigest() != config.tokenizer_sha256:
                raise ValueError("Tokenizer artifact mismatch")
            self.tokenizer = Tokenizer.from_file(str(path))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        except Exception:
            raise ChunkError("CHUNK_TOKENIZER_LOAD_FAILED") from None

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return list(self.tokenizer.encode(text, add_special_tokens=False).offsets)
