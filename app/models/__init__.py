"""Importing this package registers every model on Base.metadata (which
Alembic needs to see)."""

from app.models.auth_session import AuthSession, RefreshToken
from app.models.conversation import Conversation, ConversationMessage
from app.models.customer import Customer
from app.models.order import CancellationRequest, Order, OrderDraft, OrderItem
from app.models.product import Product, ProductType
from app.models.ticket import Ticket

__all__ = [
    "AuthSession",
    "CancellationRequest",
    "Conversation",
    "ConversationMessage",
    "Customer",
    "Order",
    "OrderDraft",
    "OrderItem",
    "Product",
    "ProductType",
    "RefreshToken",
    "Ticket",
]
