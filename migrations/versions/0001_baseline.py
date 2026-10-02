"""Baseline: customers, orders, tickets as they existed before Alembic.

These tables were originally created by schema.sql. Every statement here is
IF NOT EXISTS, so this is a no-op on a database that already has them and
creates them on a fresh one -- either way both end up at the same starting
point for the next migration.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            password_hash TEXT
        )
        """
    )
    op.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS password_hash TEXT")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            item TEXT NOT NULL,
            status TEXT NOT NULL,
            amount NUMERIC(10, 2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            subject TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError("The baseline cannot be downgraded.")
