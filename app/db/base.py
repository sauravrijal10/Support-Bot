from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared base for all ORM models. Alembic diffs `Base.metadata`."""
