"""Importing this package registers every model on Base.metadata (which
Alembic needs to see)."""

from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product, ProductType
from app.models.ticket import Ticket

__all__ = ["Customer", "Order", "OrderItem", "Product", "ProductType", "Ticket"]
