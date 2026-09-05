"""Isolated, time-bounded structural check only. No text, OCR or page extraction."""

import logging
import sys
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import DictionaryObject


def structurally_valid(path: Path) -> bool:
    logging.disable(logging.CRITICAL)
    try:
        with path.open("rb") as source:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                return False
            root = reader.trailer["/Root"]
            if not isinstance(root, DictionaryObject):
                return False
            if root.get("/Type") != "/Catalog" or any(
                key in root for key in ("/OpenAction", "/AA")
            ):
                return False
            names = root.get("/Names")
            if names and any(
                key in names.get_object() for key in ("/JavaScript", "/EmbeddedFiles")
            ):
                return False
            pages = root["/Pages"]
            if not isinstance(pages, DictionaryObject):
                return False
            return pages.get("/Type") == "/Pages" and int(pages.get("/Count", 0)) > 0
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(0 if structurally_valid(Path(sys.argv[1])) else 1)
