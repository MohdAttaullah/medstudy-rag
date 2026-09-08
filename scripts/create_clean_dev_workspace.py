"""Create an additional, empty local development workspace. Deletes nothing.

    uv run python scripts/create_clean_dev_workspace.py            # dry run (default)
    uv run python scripts/create_clean_dev_workspace.py --apply
    uv run python scripts/create_clean_dev_workspace.py --list

A "workspace" here is a tenant. `MEDRAG_DEV_PRINCIPALS` is already a list of credentials that each
carry their own `tenant_id`, and `ensure_actor` creates the tenant and user rows idempotently on the
first authenticated request. So a clean Library needs no migration, no deletion and no new endpoint:
it needs two more credentials pointing at a tenant id nobody has used yet.

**Nothing existing is removed.** The current workspace and its fixtures stay exactly where they are,
reachable at any time by pasting its old access key back into the sign-in box. That is why this is
the safe option: the repository's deletion story is deliberately unfinished, because a verified
answer is bound to the citations it was verified against, and this utility does not touch that
question at all.

Development only. It refuses to run when the environment is production or when production
authentication is configured, because a tool that mints static never-expiring credentials has no
business anywhere near a real deployment.
"""

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ENV_PATH = ROOT / ".env"
ACCESS_PATH = ROOT / ".local" / "dev-access.txt"
VARIABLE = "MEDRAG_DEV_PRINCIPALS"


def fail(message: str) -> None:
    print(f"Refusing to continue: {message}", file=sys.stderr)
    raise SystemExit(2)


def guard_development_only() -> Any:
    """Three independent refusals. Any one of them is enough to stop.

    The settings object is loaded rather than the raw file parsed, so this honours exactly the
    same configuration the application would see — including the M12 production validator, which
    would itself have refused to construct a production configuration carrying dev principals.
    """
    from app.core.config import Settings

    try:
        settings = Settings(_env_file=str(ENV_PATH) if ENV_PATH.exists() else None)
    except Exception as exc:  # noqa: BLE001 - a config that will not load is a stop condition
        fail(f"the current configuration does not load ({type(exc).__name__}).")

    if settings.environment == "production":
        fail("MEDRAG_ENVIRONMENT is 'production'. This utility is development-only.")
    if settings.auth.mode != "development":
        fail(
            f"MEDRAG_AUTH__MODE is '{settings.auth.mode}'. Development credentials are meaningless "
            "when the application authenticates against an identity provider."
        )
    if not ENV_PATH.exists():
        fail(f"{ENV_PATH.name} does not exist. Run scripts/init_local_env.py first.")
    return settings


def read_principals() -> list[dict[str, Any]]:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{VARIABLE}="):
            raw = line.split("=", 1)[1].strip().strip("'").strip('"')
            try:
                return list(json.loads(raw or "[]"))
            except json.JSONDecodeError:
                fail(f"{VARIABLE} in .env is not valid JSON. Fix it by hand before continuing.")
    return []


def workspaces(principals: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for principal in principals:
        grouped.setdefault(str(principal.get("tenant_id")), []).append(principal)
    return grouped


def document_counts(settings: Any) -> dict[str, tuple[int, int]]:
    """(active, archived) documents per tenant, so the report says where the fixtures live.

    Both numbers are shown because they answer different questions. The Library view lists only
    active documents, so "active" is what you will see after signing in; archived rows still
    exist, and reporting only the total would make a workspace whose Library is empty look
    occupied. Best-effort: the database may simply not be running, and that is not a reason to
    refuse to create a workspace.
    """
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(settings.database_url.get_secret_value())
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT tenant_id, "
                        "count(*) FILTER (WHERE archived_at IS NULL), "
                        "count(*) FILTER (WHERE archived_at IS NOT NULL) "
                        "FROM documents GROUP BY tenant_id"
                    )
                ).all()
            return {str(tenant): (int(active), int(archived)) for tenant, active, archived in rows}
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 - reporting aid only
        return {}


def describe(principals: list[dict[str, Any]], counts: dict[str, tuple[int, int]]) -> None:
    grouped = workspaces(principals)
    if not grouped:
        print("No development workspaces are configured yet.")
        return
    print(f"{len(grouped)} development workspace(s) currently configured:\n")
    for tenant, members in grouped.items():
        active, archived = counts.get(tenant, (0, 0))
        if not counts:
            documents = "database unavailable"
        else:
            documents = f"Library shows {active} document(s)"
            if archived:
                documents += f", plus {archived} archived"
        print(f"  workspace {tenant}  —  {documents}")
        for member in members:
            # Never the token. A fingerprint is enough to match a key you already hold.
            token = str(member.get("token", ""))
            fingerprint = f"{token[:4]}…{token[-4:]}" if len(token) > 8 else "(short)"
            print(
                f"      {member.get('role', '?'):<8} {member.get('display_name', '?'):<24} "
                f"key {fingerprint}"
            )
        print()


def build_workspace(label: str, with_reader: bool) -> list[dict[str, Any]]:
    tenant = str(uuid4())
    people = [(f"{label} curator", "admin")]
    if with_reader:
        people.append((f"{label} reader", "reader"))
    return [
        {
            "token": token_urlsafe(32),
            "user_id": str(uuid4()),
            "tenant_id": tenant,
            "display_name": name,
            "role": role,
        }
        for name, role in people
    ]


def write_env(principals: list[dict[str, Any]]) -> Path:
    """Rewrite only the one variable, after taking a timestamped backup."""
    # Named `.env.backup-<stamp>`, which `.gitignore` already covers with `.env.*`.
    backup = ENV_PATH.parent / f".env.backup-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    shutil.copy2(ENV_PATH, backup)
    lines = [
        line
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{VARIABLE}=")
    ]
    lines.append(f"{VARIABLE}='" + json.dumps(principals, separators=(",", ":")) + "'")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return backup


def write_access_file(created: list[dict[str, Any]], label: str) -> None:
    """Append the new keys to the ignored access file, preserving what is already there."""
    ACCESS_PATH.parent.mkdir(exist_ok=True)
    existing = (
        ACCESS_PATH.read_text(encoding="utf-8")
        if ACCESS_PATH.exists()
        else ("Development access keys. Do not share or commit.\n")
    )
    block = [
        f"\n\n--- {label} workspace (tenant {created[0]['tenant_id']}) "
        f"created {datetime.now(UTC):%Y-%m-%d %H:%M UTC} ---"
    ]
    block += [f"\n{item['display_name']} ({item['role']})\n{item['token']}" for item in created]
    ACCESS_PATH.write_text(existing.rstrip("\n") + "".join(block) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the new credentials. Without this nothing is changed.",
    )
    parser.add_argument("--list", action="store_true", help="Show configured workspaces and exit.")
    parser.add_argument("--label", default="Clean testing", help="Name shown in the session bar.")
    parser.add_argument("--no-reader", action="store_true", help="Create only the admin identity.")
    args = parser.parse_args()

    settings = guard_development_only()
    principals = read_principals()
    counts = document_counts(settings)

    print("Current state")
    print("-------------")
    describe(principals, counts)

    if args.list:
        return

    created = build_workspace(args.label, not args.no_reader)
    tenant = created[0]["tenant_id"]

    print("Planned change")
    print("--------------")
    print(f"  Add {len(created)} credential(s) for a NEW empty workspace {tenant}:")
    for item in created:
        print(f"      {item['role']:<8} {item['display_name']}")
    print(f"  Keep all {len(principals)} existing credential(s) exactly as they are.")
    print("  Create no tenant row here: the application does that on the first signed-in request.")
    print("  Delete nothing. No document, object, vector, index or audit record is touched.")
    print()

    if not args.apply:
        print("DRY RUN — nothing was written. Re-run with --apply to make these changes.")
        return

    backup = write_env(principals + created)
    write_access_file(created, args.label)

    print("Applied")
    print("-------")
    print(f"  .env updated; previous file saved as {backup.name}")
    print(f"  New keys appended to {ACCESS_PATH.relative_to(ROOT)} (git-ignored)")
    print()
    print("Restart the API so it reads the new credentials:")
    print("    docker compose --profile app --profile workers up -d --force-recreate api")
    print()
    print(f"Then sign in with the '{args.label} curator' key from the access file.")
    print("Your existing workspace and its fixtures are untouched and remain reachable with the")
    print("key you were using before.")


if __name__ == "__main__":
    main()
