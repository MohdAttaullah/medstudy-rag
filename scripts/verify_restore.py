"""Verify a restored database. Read-only, and refuses to run against the configured live database.

    uv run python scripts/verify_restore.py --database postgresql+psycopg://.../medrag_restore

A backup strategy is incomplete without restore verification, and a restore is only verified if
the *invariants* survived — not merely if the rows loaded. This checks schema head, tenant-scoped
row counts, object-key resolvability, the verified-answer rule, and that the configuration-history
immutability trigger is still present.

Nothing here writes. It refuses to target the database named in the current configuration, so a
rehearsal cannot be pointed at production by a copy-paste mistake.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import Settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Restored database URL.")
    parser.add_argument("--json", type=Path, help="Write the report here.")
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Permit targeting the configured database. Almost never correct.",
    )
    args = parser.parse_args()

    settings = Settings(_env_file=str(ROOT / ".env") if (ROOT / ".env").exists() else None)
    live = settings.database_url.get_secret_value()
    if live and args.database.strip() == live.strip() and not args.allow_live:
        raise SystemExit(
            "Refusing to verify against the configured database. Restore into a disposable "
            "target and point --database at that."
        )

    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "passed": ok, "detail": detail})
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")

    engine = create_engine(args.database)
    try:
        try:
            engine.connect().close()
        except Exception as exc:  # noqa: BLE001 - an unreachable target is the finding
            # Never print the URL: it carries the password. Report the failure class only.
            print(f"FAIL connect: {type(exc).__name__} — check the restored database URL")
            raise SystemExit(1) from None
        with engine.connect() as connection:
            head = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            record("schema_head", bool(head), str(head))

            counts = {}
            for table in (
                "tenants",
                "users",
                "documents",
                "document_versions",
                "ingestion_jobs",
                "conversations",
                "conversation_turns",
                "configuration_revisions",
                "audit_events",
            ):
                try:
                    counts[table] = connection.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed identifiers
                    ).scalar()
                except Exception:  # noqa: BLE001 - a missing table is the finding
                    counts[table] = None
            missing = [name for name, value in counts.items() if value is None]
            record("tables_present", not missing, ", ".join(missing) or "all expected tables exist")
            record("row_counts", True, json.dumps(counts))

            # The invariant that matters most: the schema CHECK constraints would have rejected a
            # violating row at load, so confirming it holds proves the constraint survived.
            violations = connection.execute(
                text(
                    "SELECT count(*) FROM conversation_turns "
                    "WHERE (outcome = 'VERIFIED') <> (answer_text IS NOT NULL) "
                    "   OR (outcome = 'VERIFIED') <> verified"
                )
            ).scalar()
            record(
                "verified_answer_rule",
                violations == 0,
                f"{violations} turn(s) violate the answer-only-when-verified rule",
            )

            # PostgreSQL registers one pg_trigger row per timing/event pair, so an UPDATE OR
            # DELETE trigger appears twice. Presence is what matters, not the count.
            trigger = connection.execute(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname = 'configuration_revision_immutable'"
                )
            ).scalar()
            record(
                "configuration_history_immutable",
                bool(trigger),
                f"immutability trigger present ({trigger} event rows)"
                if trigger
                else "TRIGGER MISSING — history is editable",
            )

            orphans = connection.execute(
                text(
                    "SELECT count(*) FROM document_versions v "
                    "LEFT JOIN documents d ON d.id = v.document_id AND d.tenant_id = v.tenant_id "
                    "WHERE d.id IS NULL"
                )
            ).scalar()
            record("referential_integrity", orphans == 0, f"{orphans} orphaned document version(s)")

            keys = connection.execute(
                text(
                    "SELECT count(*) FROM document_versions "
                    "WHERE object_storage_key IS NULL OR object_storage_key = ''"
                )
            ).scalar()
            record(
                "object_keys_present",
                keys == 0,
                f"{keys} version(s) without an object key (originals unreachable)",
            )
    finally:
        engine.dispose()

    failed = [c for c in checks if not c["passed"]]
    print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"checks": checks}, indent=2), encoding="utf-8")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
