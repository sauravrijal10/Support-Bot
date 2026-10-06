"""Cancellation requests: the support bot's "cancel this order?" awaiting a yes.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cancellation_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'done', 'superseded', 'expired')",
            name="ck_cancellation_requests_status",
        ),
    )
    op.create_index(
        "ix_cancellation_requests_customer_id", "cancellation_requests", ["customer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_cancellation_requests_customer_id", table_name="cancellation_requests")
    op.drop_table("cancellation_requests")
