"""Product variants stored as JSONB on the product.

* products.variants: JSONB list of {sku, options, price, stock_quantity,
  is_active}, default []; GIN index (jsonb_path_ops) to find a variant's
  product by SKU
* order_items.variant_sku: which variant was ordered (NULL = no variants)

Existing products keep variants = [], so they behave exactly as before.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("variants", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_index(
        "ix_products_variants", "products", ["variants"],
        postgresql_using="gin", postgresql_ops={"variants": "jsonb_path_ops"},
    )
    op.add_column("order_items", sa.Column("variant_sku", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("order_items", "variant_sku")
    op.drop_index("ix_products_variants", table_name="products")
    op.drop_column("products", "variants")
