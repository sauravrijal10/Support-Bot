from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import TICKET_STATUSES, sql_in


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        sa.CheckConstraint(sql_in("status", TICKET_STATUSES), name="ck_tickets_status"),
        sa.Index("ix_tickets_customer_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(sa.ForeignKey("customers.id"))
    subject: Mapped[str] = mapped_column(sa.Text)
    description: Mapped[str] = mapped_column(sa.Text, server_default="")
    status: Mapped[str] = mapped_column(sa.Text, server_default="open")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )
