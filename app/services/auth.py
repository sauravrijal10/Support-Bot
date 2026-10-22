"""
app/services/auth.py

Password hashing (argon2) and access-token (JWT) helpers.

Hashing functions don't touch settings so scripts like seed_data.py can
use them without needing the full app environment.
"""

from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

ALGORITHM = "HS256"

_hasher = PasswordHasher()

# Verified against when a login names an unknown/passwordless account, so
# "no such user" costs the same as "wrong password" and response time
# doesn't reveal which emails are registered.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(
            password_hash
        )
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(customer_id: int, session_id: int) -> tuple[str, int]:
    """Returns (token, lifetime_in_seconds). Short-lived: the session's
    refresh token renews it (app/services/sessions.py). `sid` names the
    session, so revoking the session invalidates the token at once."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    lifetime = timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(customer_id), "sid": str(session_id), "iat": now, "exp": now + lifetime}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> tuple[int, int]:
    """Returns (customer_id, session_id). Raises jwt.InvalidTokenError
    (covers expired, tampered and malformed tokens, and older tokens with no
    session) or ValueError."""
    payload = jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub", "sid"]},
    )
    return int(payload["sub"]), int(payload["sid"])
