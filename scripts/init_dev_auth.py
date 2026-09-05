"""Provision local auth keys without printing secrets or rotating existing identities."""

import json
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
path = root / ".env"
if not path.exists():
    raise SystemExit("Run scripts/init_local_env.py first.")
existing = dotenv_values(path).get("MEDRAG_DEV_PRINCIPALS")
if existing and json.loads(existing):
    print("Existing development identities preserved.")
else:
    tenant = str(uuid4())
    credentials = [
        {
            "token": token_urlsafe(32),
            "user_id": str(uuid4()),
            "tenant_id": tenant,
            "display_name": name,
            "role": role,
        }
        for name, role in (("Local curator", "admin"), ("Local reader", "reader"))
    ]
    lines = [
        line
        for line in path.read_text().splitlines()
        if not line.startswith("MEDRAG_DEV_PRINCIPALS=")
    ]
    lines.append("MEDRAG_DEV_PRINCIPALS='" + json.dumps(credentials, separators=(",", ":")) + "'")
    path.write_text("\n".join(lines) + "\n")
    access = root / ".local/dev-access.txt"
    access.parent.mkdir(exist_ok=True)
    access.write_text(
        "Development access keys. Do not share or commit.\n\n"
        + "\n\n".join(
            item["display_name"] + " (" + item["role"] + ")\n" + item["token"]
            for item in credentials
        )
        + "\n"
    )
    print("Created development identities. Keys are in ignored .local/dev-access.txt.")
