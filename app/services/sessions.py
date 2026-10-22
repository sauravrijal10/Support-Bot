"""
app/services/sessions.py

Sign-in sessions and refresh tokens.

Access tokens (app/services/auth.py) are short-lived JWTs naming a session
(`sid`). A session's refresh token gets a new access token without the
password, and every refresh ROTATES it: the token is marked used and a new
one issued. So:

* Signing out revokes the session: its refresh token stops working, and
  because every request checks the session (active_customer), so do its
  access tokens -- at once, not when they expire.
* A used refresh token presented again means a copy exists somewhere else
  (stolen, or replayed). The whole session is revoked, signing out the thief
  and the customer alike, who simply logs in again. Within REUSE_GRACE of
  the rotation it is only refused, so two browser tabs refreshing at the
  same moment don't sign each other out.
* Refreshing never extends a session past SESSION_LIFETIME.

Refresh tokens are random (not JWTs) and stored only as SHA-256 hashes: a
database leak doesn't hand out working tokens.

Like orders.py, functions here don't commit; the caller does.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuthSession, Customer, RefreshToken
from app.services.errors import UnauthorizedError

SESSION_LIFETIME = timedelta(days=30)
REUSE_GRACE = timedelta(seconds=30)
INVALID_REFRESH = "Invalid or expired refresh token. Please sign in again."


@dataclass
class IssuedRefresh:
    session_id: int
    customer_id: int
    token: str  # shown to the client once; only its hash is stored
    expires_at: datetime


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _issue(session: Session, auth_session: AuthSession, now: datetime) -> IssuedRefresh:
    token = secrets.token_urlsafe(32)
    expires_at = min(now + timedelta(days=get_settings().refresh_token_days), auth_session.expires_at)
    session.add(RefreshToken(
        session_id=auth_session.id, token_hash=_hash(token), created_at=now, expires_at=expires_at,
    ))
    session.flush()
    return IssuedRefresh(auth_session.id, auth_session.customer_id, token, expires_at)


def start_session(session: Session, customer_id: int, now: datetime) -> IssuedRefresh:
    """A new sign-in (after the password was checked)."""
    auth_session = AuthSession(
        customer_id=customer_id, created_at=now, expires_at=now + SESSION_LIFETIME
    )
    session.add(auth_session)
    session.flush()
    return _issue(session, auth_session, now)


def rotate(session: Session, token: str, now: datetime) -> IssuedRefresh:
    """Exchange a refresh token for the next one. Raises UnauthorizedError
    for anything but a current token of a live session -- and revokes the
    session when a used token comes back after REUSE_GRACE."""
    # FOR UPDATE: two requests with the same token can't both rotate it.
    row = session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == _hash(token)).with_for_update()
    )
    if row is None:
        raise UnauthorizedError(INVALID_REFRESH)
    auth_session = row.session
    if auth_session.revoked_at is not None or auth_session.expires_at <= now:
        raise UnauthorizedError(INVALID_REFRESH)
    if row.used_at is not None:
        if now - row.used_at > REUSE_GRACE:
            auth_session.revoked_at = now  # a replayed token: assume theft
        raise UnauthorizedError(INVALID_REFRESH)
    if row.expires_at <= now:
        raise UnauthorizedError(INVALID_REFRESH)
    row.used_at = now
    return _issue(session, auth_session, now)


def revoke(session: Session, token: str, now: datetime) -> None:
    """Sign out the session this refresh token belongs to. An unknown or
    already-revoked token is not an error: signing out is idempotent."""
    row = session.scalar(select(RefreshToken).where(RefreshToken.token_hash == _hash(token)))
    if row is not None and row.session.revoked_at is None:
        row.session.revoked_at = now


def revoke_all(session: Session, customer_id: int, now: datetime) -> None:
    """Sign the customer out everywhere."""
    session.execute(
        update(AuthSession)
        .where(AuthSession.customer_id == customer_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def active_customer(
    session: Session, customer_id: int, session_id: int, now: datetime
) -> Customer | None:
    """The customer, if `session_id` is their own live session. Checked on
    every authenticated request, so a revoked session's access tokens stop
    working immediately."""
    return session.scalar(
        select(Customer)
        .join(AuthSession, AuthSession.customer_id == Customer.id)
        .where(
            Customer.id == customer_id,
            AuthSession.id == session_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
    )
