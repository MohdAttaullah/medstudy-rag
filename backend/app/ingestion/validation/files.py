import hashlib
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import BinaryIO

from app.core.errors import DomainError
from app.core.ingestion_config import IngestionConfig


def sanitize_filename(filename: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in filename):
        raise DomainError("UPLOAD_INVALID_FILENAME", "The filename contains invalid characters.")
    if "/" in filename or "\\" in filename or ":" in filename:
        raise DomainError("UPLOAD_INVALID_FILENAME", "Use a filename without directory paths.")
    if not filename.lower().endswith(".pdf") or filename.lower().endswith(".pdf.pdf"):
        raise DomainError(
            "UPLOAD_UNSUPPORTED_TYPE", "Only files with a .pdf extension are accepted."
        )
    stem = unicodedata.normalize("NFKD", filename[:-4]).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:180]
    return (stem or "document") + ".pdf"


def validate_mime(mime: str | None, config: IngestionConfig) -> None:
    if mime not in config.allowed_mime_types:
        raise DomainError(
            "UPLOAD_UNSUPPORTED_TYPE", "The upload must have application/pdf type.", 415
        )


def checksum(stream: BinaryIO, chunk_size: int = 65536) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while block := stream.read(chunk_size):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


def validate_pdf(path: Path, config: IngestionConfig) -> None:
    with path.open("rb") as stream:
        if not stream.read(8).startswith(b"%PDF-"):
            raise DomainError("UPLOAD_INVALID_PDF", "The file does not have a valid PDF signature.")
        stream.seek(max(0, path.stat().st_size - 1024))
        if b"%%EOF" not in stream.read():
            raise DomainError("UPLOAD_INVALID_PDF", "The PDF appears incomplete.")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.ingestion.validation.pdf_check", str(path)],
            capture_output=True,
            timeout=config.validation_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DomainError(
            "UPLOAD_VALIDATION_TIMEOUT", "PDF validation exceeded its time limit."
        ) from None
    if result.returncode != 0:
        raise DomainError(
            "UPLOAD_INVALID_PDF",
            "The PDF is malformed, encrypted, or contains unsupported active content.",
        )
