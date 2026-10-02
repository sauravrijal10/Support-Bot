from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import BILLING_PERIODS, sql_in


class ProductType(Base):
    """A category of product (books, clothing, ...) and the attributes its
    products carry. `attribute_schema` is a list of attribute definitions
    (see app/schemas/product_type.py); every product's `attributes` is
    checked against it by app/services/catalog.py."""

    __tablename__ = "product_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String(64), unique=True)
    name: Mapped[str] = mapped_column(sa.String(100))
    description: Mapped[str] = mapped_column(sa.Text, server_default="")
    attribute_schema: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'::jsonb"))
    # Inactive types are hidden from the type listing and can't get new
    # products. Their existing products are governed by Product.is_active.
    is_active: Mapped[bool] = mapped_column(server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Product(Base):
    """Something a customer can buy: a plan, a book, a T-shirt, a drive...
    Its type decides which `attributes` it has."""

    __tablename__ = "products"
    __table_args__ = (
        sa.CheckConstraint("price >= 0", name="ck_products_price_nonneg"),
        sa.CheckConstraint(
            "stock_quantity IS NULL OR stock_quantity >= 0",
            name="ck_products_stock_nonneg",
        ),
        sa.CheckConstraint(
            sql_in("billing_period", BILLING_PERIODS), name="ck_products_billing_period"
        ),
        sa.Index("ix_products_product_type_id", "product_type_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(sa.String(64), unique=True)
    product_type_id: Mapped[int] = mapped_column(sa.ForeignKey("product_types.id"))
    name: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str] = mapped_column(sa.Text, server_default="")
    # Type-specific values, e.g. {"author": "...", "format": "paperback"}.
    attributes: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"))
    price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    billing_period: Mapped[str] = mapped_column(sa.String(16), server_default="one_time")
    # NULL = unlimited (digital goods). A number = units left.
    stock_quantity: Mapped[int | None]
    is_active: Mapped[bool] = mapped_column(server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )

    # joined: the type is shown with every product, so load it in the same
    # query. innerjoin: the FK is NOT NULL, and Postgres refuses FOR UPDATE
    # (used when ordering) on the nullable side of an outer join.
    product_type: Mapped[ProductType] = relationship(lazy="joined", innerjoin=True)
