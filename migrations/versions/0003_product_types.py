"""Product types: every product belongs to a type that defines its attributes.

* product_types: new table (slug, name, attribute_schema JSONB, ...)
* products: product_type_id (NOT NULL, FK), attributes JSONB

Existing products are assigned to a "general" type with no attributes, so
nothing is lost; seed_data.py (or an admin) can move them to real types.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "product_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "attribute_schema", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("slug", name="product_types_slug_key"),
    )
    op.execute(
        "INSERT INTO product_types (slug, name, description) "
        "VALUES ('general', 'General', 'Products without a more specific type.')"
    )

    op.add_column("products", sa.Column("product_type_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE products SET product_type_id = (SELECT id FROM product_types WHERE slug = 'general')"
    )
    op.alter_column("products", "product_type_id", nullable=False)
    op.create_foreign_key(
        "products_product_type_id_fkey", "products", "product_types", ["product_type_id"], ["id"]
    )
    op.create_index("ix_products_product_type_id", "products", ["product_type_id"])

    op.add_column(
        "products",
        sa.Column("attributes", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("products", "attributes")
    op.drop_index("ix_products_product_type_id", table_name="products")
    op.drop_constraint("products_product_type_id_fkey", "products", type_="foreignkey")
    op.drop_column("products", "product_type_id")
    op.drop_table("product_types")
