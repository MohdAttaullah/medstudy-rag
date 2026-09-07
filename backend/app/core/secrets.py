"""Where a secret comes from in production, without binding the architecture to one vendor.

Every managed secret store this system might plausibly run against — Azure Key Vault via the CSI
driver, AWS Secrets Manager via External Secrets, HashiCorp Vault Agent, Kubernetes Secrets, Docker
secrets — ends by presenting the secret as a file on disk. Supporting `<NAME>_FILE` therefore
supports all of them and depends on none of them, which is the only way to stay vendor-neutral
without writing five SDK integrations that would each need credentials of their own.

The indirection also removes the secret from the process environment, where it would otherwise be
readable from `/proc/<pid>/environ`, inherited by every subprocess, and printed by any crash
reporter that dumps the environment.

This module resolves nothing itself at import time. It is called once, before `Settings` is
constructed, so the values reach the existing `SecretStr` fields by the ordinary path and every
downstream protection that already applies to them continues to apply.
"""

import os
from pathlib import Path

#: Secrets this system understands. A name here may be supplied directly or as `<NAME>_FILE`.
#: Deliberately explicit: resolving every `*_FILE` variable in the environment would let an
#: unrelated variable name become a file read.
SECRET_VARIABLES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "MEDRAG_OPENAI_API_KEY",
    "MEDRAG_ANTHROPIC_API_KEY",
    "MEDRAG_DATABASE_URL",
    "MEDRAG_REDIS_URL",
    "MEDRAG_QDRANT_API_KEY",
    "MEDRAG_S3_ACCESS_KEY",
    "MEDRAG_S3_SECRET_KEY",
    "MEDRAG_AUTH__OIDC_CLIENT_SECRET",
    "MEDRAG_DEV_PRINCIPALS",
)

#: Largest secret file this will read. A mount that is accidentally a log file should be refused
#: rather than loaded into memory and then into a connection string.
MAX_SECRET_BYTES = 64 * 1024


class SecretResolutionError(RuntimeError):
    """A declared secret file could not be used. The message never contains the value."""


def resolve_secret_files(
    environ: dict[str, str] | None = None, *, variables: tuple[str, ...] = SECRET_VARIABLES
) -> tuple[str, ...]:
    """Read `<NAME>_FILE` into `<NAME>` for each known secret. Returns the names resolved.

    A direct value already present wins and the file is ignored, so a developer's `.env` keeps
    working unchanged and a deployment that mounts files does not need a different code path.
    Trailing newlines are stripped because every secret store adds one and no secret here ends in
    whitespace.
    """
    env = os.environ if environ is None else environ
    resolved: list[str] = []
    for name in variables:
        pointer = env.get(f"{name}_FILE", "").strip()
        if not pointer or env.get(name, "").strip():
            continue
        path = Path(pointer)
        try:
            if path.stat().st_size > MAX_SECRET_BYTES:
                raise SecretResolutionError(
                    f"{name}_FILE is larger than {MAX_SECRET_BYTES} bytes; refusing to read it."
                )
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            # The path is operator-supplied configuration, not a secret, so naming it is safe and
            # is the only way to make a mount typo diagnosable.
            raise SecretResolutionError(
                f"{name}_FILE could not be read: {path} ({exc.strerror})"
            ) from None
        if not value:
            raise SecretResolutionError(f"{name}_FILE is empty: {path}")
        env[name] = value
        resolved.append(name)
    return tuple(resolved)


def secret_sources(environ: dict[str, str] | None = None) -> dict[str, str]:
    """How each known secret was supplied, for sanitized preflight output. Never a value."""
    env = os.environ if environ is None else environ
    sources: dict[str, str] = {}
    for name in SECRET_VARIABLES:
        if env.get(f"{name}_FILE", "").strip():
            sources[name] = "file"
        elif env.get(name, "").strip():
            sources[name] = "environment"
        else:
            sources[name] = "absent"
    return sources
