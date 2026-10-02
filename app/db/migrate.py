from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def run_migrations() -> None:
    """Apply all pending Alembic migrations (`alembic upgrade head`).

    Reads DATABASE_URL from the environment (see migrations/env.py).
    Convenient for development; with several app instances, run this as a
    single deploy step instead so they don't race each other.
    """
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
