from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Conversation(Base):
    """A signed-in customer's saved chat (see app/services/conversations.py)."""

    __tablename__ = "conversations"
    __table_args__ = (sa.Index("ix_conversations_customer_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(sa.ForeignKey("customers.id"))
    # The first question, shortened -- for listing past conversations.
    title: Mapped[str] = mapped_column(sa.Text)
    # App clock, like OrderDraft.created_at, so tests can control time.
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))

    # passive_deletes: the database's ON DELETE CASCADE removes the messages.
    messages: Mapped[list["ConversationMessage"]] = relationship(
        order_by="ConversationMessage.id", cascade="all, delete-orphan", passive_deletes=True
    )


class ConversationMessage(Base):
    """One exchange: the customer's question and the bot's answer."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        sa.Index("ix_conversation_messages_conversation_id", "conversation_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE")
    )
    question: Mapped[str] = mapped_column(sa.Text)
    answer: Mapped[str] = mapped_column(sa.Text)
    sources: Mapped[list] = mapped_column(JSONB, server_default="[]")
    tools_used: Mapped[list] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
