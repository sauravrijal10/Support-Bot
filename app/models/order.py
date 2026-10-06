from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    CANCELLATION_REQUEST_STATUSES,
    ORDER_DRAFT_STATUSES,
    ORDER_STATUSES,
    sql_in,
)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        sa.CheckConstraint(sql_in("status", ORDER_STATUSES), name="ck_orders_status"),
        sa.Index("ix_orders_customer_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(sa.ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(sa.Text, server_default="pending")
    total_amount: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )

    # selectin: items always load with the order in one extra query, so
    # serializing an order never triggers lazy loads on a closed session.
    items: Mapped[list["OrderItem"]] = relationship(
        cascade="all, delete-orphan", order_by="OrderItem.id", lazy="selectin"
    )


class OrderItem(Base):
    """One line of an order. Name and price are copied from the product at
    purchase time, so later price changes or renames don't rewrite history."""

    __tablename__ = "order_items"
    __table_args__ = (
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_price >= 0", name="ck_order_items_price_nonneg"),
        sa.Index("ix_order_items_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(sa.ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(sa.ForeignKey("products.id"))
    # Which variant was bought (NULL for products without variants). Kept as
    # the SKU: variants live in products.variants JSON, so there's no FK.
    variant_sku: Mapped[str | None] = mapped_column(sa.String(64))
    # Snapshot, including the variant, e.g. "Acme logo T-shirt (Size: M, Colour: Black)".
    product_name: Mapped[str] = mapped_column(sa.String(200))
    unit_price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    quantity: Mapped[int]


class OrderDraft(Base):
    """A quote the support bot prepared for a customer, waiting for them to
    confirm it in a later message (see app/services/order_drafts.py).
    Nothing is reserved: confirming re-checks stock and price."""

    __tablename__ = "order_drafts"
    __table_args__ = (
        sa.CheckConstraint(sql_in("status", ORDER_DRAFT_STATUSES), name="ck_order_drafts_status"),
        sa.Index("ix_order_drafts_customer_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(sa.ForeignKey("customers.id"))
    # [{"product_id", "sku", "name", "unit_price", "quantity"}], prices as strings.
    lines: Mapped[list] = mapped_column(JSONB)
    total_amount: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    status: Mapped[str] = mapped_column(sa.Text, server_default="open")
    order_id: Mapped[int | None] = mapped_column(sa.ForeignKey("orders.id"))
    # Set from the app clock (not the DB's), because confirming compares it
    # with when the current chat request started -- also app clock.
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class CancellationRequest(Base):
    """The support bot asked a customer "cancel this order?" and is waiting
    for them to confirm in a later message (app/services/order_cancellations.py)."""

    __tablename__ = "cancellation_requests"
    __table_args__ = (
        sa.CheckConstraint(
            sql_in("status", CANCELLATION_REQUEST_STATUSES), name="ck_cancellation_requests_status"
        ),
        sa.Index("ix_cancellation_requests_customer_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(sa.ForeignKey("customers.id"))
    order_id: Mapped[int] = mapped_column(sa.ForeignKey("orders.id"))
    status: Mapped[str] = mapped_column(sa.Text, server_default="open")
    # App clock, for the same reason as OrderDraft.created_at.
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
