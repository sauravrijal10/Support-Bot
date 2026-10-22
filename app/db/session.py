from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker


def sqlalchemy_url(database_url: str) -> URL:
    """DATABASE_URL with the psycopg2 driver named explicitly.

    SQLAlchemy 2.1 maps a bare "postgresql://" to psycopg 3, which isn't
    installed. DATABASE_URL itself stays bare because the pgvector store
    passes it straight to psycopg2.connect(), which rejects "+psycopg2".
    """
    url = make_url(database_url)
    # Some hosts hand out "postgres://", which SQLAlchemy doesn't accept.
    if url.drivername in ("postgresql", "postgres"):
        url = url.set(drivername="postgresql+psycopg2")
    return url


def create_session_factory(database_url: str):
    """Returns (engine, session_factory).

    The engine pools connections, so requests reuse them instead of opening
    a new one per query. expire_on_commit=False keeps loaded attributes
    readable after commit (responses are serialized after the commit).
    """
    engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
    return engine, sessionmaker(engine, expire_on_commit=False)
