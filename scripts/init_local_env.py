"""Generate local-only credentials without printing them or overwriting an existing .env."""

from pathlib import Path
from secrets import token_hex

root = Path(__file__).resolve().parents[1]
database_password = token_hex(24)
storage_password = token_hex(24)
values = {
    "POSTGRES_PASSWORD": database_password,
    "MINIO_ROOT_PASSWORD": storage_password,
    "MEDRAG_DATABASE_URL": (
        f"postgresql+psycopg://medrag:{database_password}@127.0.0.1:5432/medrag"
    ),
    "MEDRAG_S3_ACCESS_KEY": "medrag-local",
    "MEDRAG_S3_SECRET_KEY": storage_password,
}
lines = []
for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
    key = line.partition("=")[0]
    lines.append(f"{key}={values[key]}" if key in values else line)
try:
    with (root / ".env").open("x", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    print("Created .env with generated local credentials. Keep it out of version control.")
except FileExistsError:
    print("Existing .env preserved.")
