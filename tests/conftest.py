"""Shared fixtures."""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.session import sqlalchemy_url
from app.services import rate_limit


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Rate-limit counters are per process; start every test from zero."""
    rate_limit.reset_all()
    yield
    rate_limit.reset_all()


@pytest.fixture
def session():
    """A session on the real Postgres (DATABASE_URL), inside a transaction that
    is rolled back afterwards. Skips the test when no database is available."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    engine = create_engine(sqlalchemy_url(url))
    try:
        connection = engine.connect()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"database not reachable: {error}")
    outer = connection.begin()
    # Commits/savepoints inside the code under test stay within `outer`.
    with Session(bind=connection, join_transaction_mode="create_savepoint") as s:
        yield s
    outer.rollback()
    connection.close()
    engine.dispose()
