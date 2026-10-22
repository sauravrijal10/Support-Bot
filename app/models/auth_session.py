from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AuthSession(Base):
    """One sign-in (a device or browser). Access tokens name it (`sid`), so
    revoking it signs that device out at once (see app/services/sessions.py)."""

    __tablename__ = "auth_sessions"
    __table_args__ = (sa.Index("ix_auth_sessions_customer_id", "customer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(sa.ForeignKey("customers.id"))
    # App clock, like OrderDraft.created_at, so tests can control time.
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    # Absolute end: refreshing never extends a session past this.
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class RefreshToken(Base):
    """A refresh token, stored only as its SHA-256 hash. Each is used once:
    refreshing marks it used and issues the next one in the same session."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        sa.Index("ix_refresh_tokens_session_id", "session_id"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        sa.ForeignKey("auth_sessions.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    session: Mapped[AuthSession] = relationship()
