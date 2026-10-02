"""Products, order line items, admin flag; orders and tickets get real structure.

* customers: is_admin, created_at
* products: new table
* order_items: new table. Orders used to hold one free-text `item` and an
  `amount`; each existing order is converted to one line item pointing at an
  inactive "legacy" product, so no order data is lost.
* orders: total_amount, updated_at, status CHECK; item/amount dropped
* tickets: description, updated_at, status CHECK

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade() -> None:
    # --- customers ---------------------------------------------------------
    op.add_column(
        "customers",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "customers",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )

    # --- products ----------------------------------------------------------
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("billing_period", sa.String(16), nullable=False, server_default="one_time"),
        sa.Column("stock_quantity", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("sku", name="products_sku_key"),
        sa.CheckConstraint("price >= 0", name="ck_products_price_nonneg"),
        sa.CheckConstraint(
            "stock_quantity IS NULL OR stock_quantity >= 0", name="ck_products_stock_nonneg"
        ),
        sa.CheckConstraint(
            "billing_period IN ('one_time', 'monthly', 'yearly')",
            name="ck_products_billing_period",
        ),
    )

    # --- orders: add new columns, keep old ones until data is moved ---------
    op.add_column(
        "orders",
        sa.Column("total_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "orders",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )

    # --- order_items -------------------------------------------------------
    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_price >= 0", name="ck_order_items_price_nonneg"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])

    # --- move legacy order data -------------------------------------------
    op.execute("UPDATE orders SET total_amount = amount")
    op.execute(
        """
        INSERT INTO products (sku, name, description, price, billing_period, is_active)
        SELECT 'legacy-' || substr(md5(item), 1, 12),
               left(item, 200),
               'Imported from a legacy order; not for sale',
               min(amount),
               'one_time',
               false
        FROM orders
        GROUP BY item
        """
    )
    op.execute(
        """
        INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity)
        SELECT o.id, p.id, left(o.item, 200), o.amount, 1
        FROM orders o
        JOIN products p ON p.sku = 'legacy-' || substr(md5(o.item), 1, 12)
        """
    )

    op.drop_column("orders", "item")
    op.drop_column("orders", "amount")
    op.alter_column("orders", "total_amount", server_default=None)
    op.create_check_constraint(
        "ck_orders_status",
        "orders",
        "status IN ('pending', 'processing', 'completed', 'cancelled')",
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])

    # --- tickets -----------------------------------------------------------
    op.add_column(
        "tickets", sa.Column("description", sa.Text(), nullable=False, server_default="")
    )
    op.add_column(
        "tickets",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_check_constraint(
        "ck_tickets_status",
        "tickets",
        "status IN ('open', 'in_progress', 'resolved', 'closed')",
    )
    op.create_index("ix_tickets_customer_id", "tickets", ["customer_id"])


def downgrade() -> None:
    raise NotImplementedError(
        "This migration converts data (orders.item/amount -> order_items) and "
        "is not reversible. Restore from a backup instead."
    )
