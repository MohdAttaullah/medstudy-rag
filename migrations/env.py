from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import Settings
from app.db.base import Base
from app.models import chunking  # noqa: F401
from app.models import conversations  # noqa: F401
from app.models import embeddings  # noqa: F401
from app.models import documents, parsing  # noqa: F401
from app.models import retrieval  # noqa: F401

target_metadata = Base.metadata
database_url = Settings().database_url.get_secret_value()
if not database_url:
    raise RuntimeError("MEDRAG_DATABASE_URL is required for migrations")

if context.is_offline_mode():
    context.configure(url=database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
