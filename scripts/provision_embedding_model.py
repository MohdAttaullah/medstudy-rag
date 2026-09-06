"""Provision the pinned biomedical embedding model into the local model cache.

    uv run --extra embedding python scripts/provision_embedding_model.py [--cache DIR]

This is the only step that needs network access. It downloads exactly the revision pinned in
`EmbeddingConfig` — never `main` — verifies the weight checksum, and prints the manifest.
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

from app.core.embedding_config import EmbeddingConfig  # noqa: E402

# Only what the encoder actually needs. pytorch_model.bin is deliberately excluded: safetensors
# loads without executing a pickle, and downloading both doubles the cache for no benefit.
FILES = (
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "special_tokens_map.json",
    "LICENSE",
)


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/models/embeddings")
    arguments = parser.parse_args()

    from huggingface_hub import snapshot_download

    config = EmbeddingConfig()
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

    weights = manifest["model.safetensors"]["sha256"]
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"\nmodel.safetensors sha256: {weights}")
    if config.verify_model_checksum and weights != config.model_checksum:
        print(
            "\nCHECKSUM MISMATCH with EmbeddingConfig.model_checksum.\n"
            "If this is a deliberate first provisioning, set the literal above to this value.\n"
            "If it is not, do not proceed: the pinned revision did not produce the expected bytes."
        )
        return 1
    (cache / "manifest.json").write_text(
        json.dumps(
            {"model": config.model_id, "revision": config.model_revision, "files": manifest},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print("\nPASS: pinned revision provisioned and checksum verified. Offline embedding is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
