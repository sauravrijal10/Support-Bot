from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ORDER_STATUSES, sql_in


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
    product_name: Mapped[str] = mapped_column(sa.String(200))
    unit_price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    quantity: Mapped[int]
