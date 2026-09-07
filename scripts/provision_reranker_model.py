"""Provision the pinned biomedical embedding model into the local model cache.

    uv run --extra embedding python scripts/provision_query_model.py [--cache DIR]

This is the only step that needs network access. It downloads exactly the revision pinned in
`RerankerConfig` â€” never `main` â€” verifies the weight checksum, and prints the manifest.
Afterwards the embedding worker runs fully offline against the cache.

In production the cache is provisioned into the image or the named volume by this script; a
runtime download on a user's first request is not an acceptable dependency.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.reranking_config import MODEL_FILES, RerankerConfig  # noqa: E402

# Official main has only PyTorch weights. Verify SHA before weights_only=True loading.
FILES = tuple(MODEL_FILES)


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/models/reranking")
    arguments = parser.parse_args()

    from huggingface_hub import snapshot_download

    config = RerankerConfig()
    cache = arguments.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    print(f"model:    {config.model_id}")
    print(f"revision: {config.model_revision}")
    print(f"cache:    {cache}")

    location = Path(
        snapshot_download(
            repo_id=config.model_id,
            revision=config.model_revision,
            cache_dir=str(cache),
            allow_patterns=list(FILES),
        )
    )

    manifest = {}
    for name in FILES:
        path = location / name
        if not path.exists():
            print(f"MISSING: {name}")
            return 1
        manifest[name] = {"sha256": digest(path), "bytes": path.stat().st_size}

    print(json.dumps(manifest, indent=2, sort_keys=True))
    for name, checksum in MODEL_FILES.items():
        if manifest[name]["sha256"] != checksum:
            raise RuntimeError("Pinned model checksum mismatch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
