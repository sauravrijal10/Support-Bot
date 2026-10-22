"""
app/api/deps.py

FastAPI dependencies that turn a bearer token into the authenticated
customer. These are plain `def` (not async) so the blocking DB call runs
in FastAPI's threadpool instead of stalling the event loop.
"""

from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from app.services.auth import decode_access_token

# auto_error=False: a missing token yields None (anonymous) rather than an
# automatic 401, so /chat can still serve FAQ answers to signed-out users.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def customer_from_token(db, token: str) -> dict | None:
    """The customer an access token belongs to, or None if the token is
    invalid or expired, or its sign-in session was revoked (signed out) or
    has ended. Blocking (DB call). Shared by the HTTP dependencies and the
    WebSocket chat, which re-checks before every question."""
    try:
        customer_id, session_id = decode_access_token(token)
    except (jwt.InvalidTokenError, ValueError):
        return None
    return db.get_session_customer(customer_id, session_id, datetime.now(timezone.utc))


def get_optional_customer(
    request: Request, token: str | None = Depends(oauth2_scheme)
) -> dict | None:
    """None if no token was sent. A token that IS sent but is invalid or
    expired raises 401 -- it is never silently downgraded to anonymous,
    so a client with a stale token finds out it must log in again."""
    if token is None:
        return None

    customer = customer_from_token(request.app.state.db, token)
    if customer is None:
        raise _unauthorized("Invalid or expired token")
    return customer


def get_current_customer(
    customer: dict | None = Depends(get_optional_customer),
) -> dict:
    if customer is None:
        raise _unauthorized("Not authenticated")
    return customer


def require_admin(customer: dict = Depends(get_current_customer)) -> dict:
    if not customer["is_admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return customer


def get_session(request: Request):
    """One ORM session per request (from the pooled engine), closed after.
    Anything not committed by the view is rolled back on close."""
    with request.app.state.session_factory() as session:
        yield session
