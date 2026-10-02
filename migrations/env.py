import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  (registers all models on Base.metadata)
from app.db.base import Base
from app.db.session import sqlalchemy_url

load_dotenv()

target_metadata = Base.metadata

# Tables that live in the database but are NOT managed by these migrations.
# `documents` is the pgvector store, self-provisioned by
# app/services/vector_knowledge_base.py.
UNMANAGED_TABLES = {"documents"}


def include_name(name, type_, parent_names):
    # Skip before reflection, so Alembic never tries to introspect the
    # pgvector column type it doesn't know about.
    if type_ == "table" and name in UNMANAGED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=sqlalchemy_url(os.environ["DATABASE_URL"]),
        target_metadata=target_metadata,
        literal_binds=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(sqlalchemy_url(os.environ["DATABASE_URL"]), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
