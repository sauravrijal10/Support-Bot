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
from starlette.requests import HTTPConnection

from app.config import trust_proxy

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


def client_ip(conn: HTTPConnection) -> str:
    """The caller's IP, for rate limits (works for requests and WebSockets).
    Behind the proxy (TRUST_PROXY) it is the LAST X-Forwarded-For entry --
    the one the proxy itself appended. Earlier entries come from the client
    and can be anything, so they are never trusted."""
    if trust_proxy():
        hops = [h.strip() for h in conn.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if hops:
            return hops[-1]
    return conn.client.host if conn.client else "unknown"


def chat_client_key(customer: dict | None, ip: str) -> str:
    """Whose chat allowance a question counts against."""
    return f"customer:{customer['id']}" if customer else f"ip:{ip}"


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
