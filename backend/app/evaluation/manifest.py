"""Identity of one evaluation run: what code, what policy, what models, what data.

Two runs are comparable only if these agree. M10 made tenant policy a per-request value, so a
number produced under one configuration revision says nothing about a number produced under
another, and a report that omits the fingerprints invites exactly that comparison. Everything
needed to tell those runs apart is captured here and written next to the results.

No credential, prompt or document text is recorded. Model *identity* is configuration; the key
that reaches the provider is not, and never appears in an artifact.
"""

import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import Settings


def git_commit(root: Path) -> dict[str, Any]:
    """The commit under evaluation, and whether the tree was dirty when it ran.

    A dirty tree is recorded rather than refused: evaluation during development is legitimate. It
    must simply never be presented as a measurement of the commit alone.
    """

    def run(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    status = run("status", "--porcelain")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status) if status is not None else None,
        "dirty_file_count": len(status.splitlines()) if status else 0,
    }


def policy_fingerprints(settings: Settings) -> dict[str, Any]:
    """Fingerprints of every policy that can change a measured number."""
    groups = (
        ("parsing", settings.parsing),
        ("chunking", settings.chunking),
        ("embedding", settings.embedding),
        ("query_encoder", settings.query_encoder),
        ("retrieval", settings.retrieval),
        ("sparse_analyzer", settings.sparse_analyzer),
        ("reranker", settings.reranker),
        ("reranking", settings.reranking),
        ("expansion", settings.expansion),
        ("evidence_budget", settings.evidence_budget),
        ("sufficiency", settings.sufficiency),
        ("grounding", settings.grounding),
        ("claim_verification", settings.claim_verification),
        ("final_verification", settings.final_verification),
        ("contradiction", settings.contradiction),
        ("repair", settings.repair),
        ("ask", settings.ask),
    )
    result: dict[str, Any] = {}
    for name, policy in groups:
        fingerprint = getattr(policy, "fingerprint", None)
        if fingerprint is not None:
            result[name] = fingerprint
    return result


def model_identity(settings: Settings) -> dict[str, Any]:
    """Declared model identity. Credential *presence* only — never a value."""

    def selection(name: str) -> dict[str, Any] | None:
        chosen = getattr(settings, name, None)
        if chosen is None:
            return None
        return {"provider": chosen.provider, "model_id": chosen.model_id}

    generator, verifier = selection("generator"), selection("verifier")
    return {
        "embedding": {
            "model_id": settings.embedding.model_id,
            "model_revision": settings.embedding.model_revision,
            "dimension": settings.embedding.embedding_dimension,
            "pooling": settings.embedding.pooling_strategy,
            "distance": settings.embedding.distance_metric,
        },
        "query_encoder": {
            "model_id": settings.query_encoder.model_id,
            "model_revision": settings.query_encoder.model_revision,
        },
        "reranker": {
            "model_id": settings.reranker.model_id,
            "model_revision": settings.reranker.model_revision,
        },
        "generator": generator,
        "verifier": verifier,
        # An unset verifier means M8 reuses the generator. Recorded explicitly because it is the
        # single largest caveat on every verification number in the report.
        "verifier_independent_of_generator": bool(
            generator and verifier and (generator["model_id"] != verifier["model_id"])
        ),
        "credentials_present": {
            "openai": bool(settings.openai_api_key.get_secret_value()),
            "anthropic": bool(settings.anthropic_api_key.get_secret_value()),
        },
    }


def environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }


def build(
    root: Path,
    settings: Settings,
    mode: str,
    dataset_manifest: dict[str, Any],
    configuration_revision: int | None = None,
) -> dict[str, Any]:
    """A run manifest another engineer can read to know exactly what was measured."""
    return {
        "run_id": uuid4().hex,
        "manifest_version": "m11-run-manifest-v1",
        "milestone": "M11",
        "mode": mode,
        "timestamp": datetime.now(UTC).isoformat(),
        "git": git_commit(root),
        "environment": environment(),
        # M10 stores tenant policy as immutable revisions. Offline evaluation runs against the
        # startup contract and records None rather than implying a tenant revision applied.
        "configuration_revision": configuration_revision,
        "policy_fingerprints": policy_fingerprints(settings),
        "models": model_identity(settings),
        "dataset_manifest_version": dataset_manifest["manifest_version"],
        "dataset_manifest_fingerprint": dataset_manifest["fingerprint"],
        "datasets": [
            {k: d[k] for k in ("dataset_id", "version", "independence", "fingerprint")}
            for d in dataset_manifest["datasets"]
        ],
        "clinically_validated": False,
    }
