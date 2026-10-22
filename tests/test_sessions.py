"""
Tests for short-lived access tokens and refresh tokens
(app/services/sessions.py, app/services/auth.py, app/api/routes/auth.py).

The session and endpoint tests need Postgres, so they run against
DATABASE_URL and are SKIPPED when it isn't set or reachable. Each runs inside
a transaction that is rolled back afterwards. With docker compose:

    docker compose run --rm app sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
"""

import os
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import deps
from app.api.routes import auth as auth_routes
from app.config import get_settings
from app.main import domain_error_handler
from app.models import AuthSession, Customer, RefreshToken
from app.services import rate_limit, sessions
from app.services.auth import ALGORITHM, create_access_token, decode_access_token, hash_password
from app.services.errors import DomainError, UnauthorizedError

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SECRET = "test-secret-" + "x" * 40


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    if not os.environ.get("DATABASE_URL"):
        monkeypatch.setenv("DATABASE_URL", "postgresql://unused")  # settings need one; DB tests skip
    monkeypatch.delenv("JWT_EXPIRE_MINUTES", raising=False)
    monkeypatch.delenv("REFRESH_TOKEN_DAYS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------- access tokens (no DB)


def test_access_tokens_are_short_lived_and_name_their_session():
    token, lifetime = create_access_token(7, 42)
    assert lifetime == 15 * 60
    assert decode_access_token(token) == (7, 42)


def test_a_token_without_a_session_is_rejected():
    # e.g. one issued before sessions existed
    now = datetime.now(timezone.utc)
    old = jwt.encode({"sub": "7", "iat": now, "exp": now + timedelta(minutes=5)}, SECRET, algorithm=ALGORITHM)
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(old)


def test_an_expired_access_token_is_rejected():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    token = jwt.encode({"sub": "7", "sid": "1", "iat": past, "exp": past + timedelta(minutes=15)},
                       SECRET, algorithm=ALGORITHM)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


# ---------------------------------------------------------------- the session rules


@pytest.fixture
def people(session):
    ada = Customer(name="Session Ada", email="session-ada@example.test", password_hash=hash_password("ada-password"))
    grace = Customer(name="Session Grace", email="session-grace@example.test")
    session.add_all([ada, grace])
    session.flush()
    return {"ada": ada.id, "grace": grace.id}


def test_only_a_hash_of_the_refresh_token_is_stored(session, people):
    issued = sessions.start_session(session, people["ada"], T0)
    [row] = session.scalars(select(RefreshToken).where(RefreshToken.session_id == issued.session_id)).all()
    assert row.token_hash != issued.token and len(row.token_hash) == 64
    assert issued.expires_at == T0 + timedelta(days=7)
    assert sessions.active_customer(session, people["ada"], issued.session_id, T0).id == people["ada"]


def test_refreshing_rotates_the_token(session, people):
    first = sessions.start_session(session, people["ada"], T0)
    second = sessions.rotate(session, first.token, T0 + timedelta(minutes=14))
    assert second.token != first.token and second.session_id == first.session_id
    third = sessions.rotate(session, second.token, T0 + timedelta(minutes=28))
    assert third.session_id == first.session_id


def test_a_used_token_sent_again_quickly_is_refused_but_the_session_survives(session, people):
    # Two tabs refreshing at once: the loser is refused, nobody is signed out.
    first = sessions.start_session(session, people["ada"], T0)
    second = sessions.rotate(session, first.token, T0)
    with pytest.raises(UnauthorizedError):
        sessions.rotate(session, first.token, T0 + timedelta(seconds=5))
    assert sessions.rotate(session, second.token, T0 + timedelta(seconds=6))


def test_a_used_token_replayed_later_signs_the_whole_session_out(session, people):
    first = sessions.start_session(session, people["ada"], T0)
    second = sessions.rotate(session, first.token, T0)
    later = T0 + sessions.REUSE_GRACE + timedelta(seconds=1)
    with pytest.raises(UnauthorizedError):
        sessions.rotate(session, first.token, later)  # stolen copy
    assert session.get(AuthSession, first.session_id).revoked_at == later
    with pytest.raises(UnauthorizedError):
        sessions.rotate(session, second.token, later)  # the latest token is dead too
    assert sessions.active_customer(session, people["ada"], first.session_id, later) is None


def test_unknown_and_expired_refresh_tokens_are_refused(session, people):
    issued = sessions.start_session(session, people["ada"], T0)
    with pytest.raises(UnauthorizedError):
        sessions.rotate(session, "not-a-real-token", T0)
    with pytest.raises(UnauthorizedError):
        sessions.rotate(session, issued.token, T0 + timedelta(days=7, seconds=1))


def test_refreshing_never_outlives_the_session(session, people):
    issued = sessions.start_session(session, people["ada"], T0)
    near_end = T0 + sessions.SESSION_LIFETIME - timedelta(days=1)
    for day in range(0, 29, 6):  # refreshed every 6 days
        issued = sessions.rotate(session, issued.token, T0 + timedelta(days=day))
    assert issued.expires_at <= T0 + sessions.SESSION_LIFETIME
    with pytest.raises(UnauthorizedError):
        sessions.rotate(session, issued.token, T0 + sessions.SESSION_LIFETIME + timedelta(seconds=1))
    assert sessions.active_customer(session, people["ada"], issued.session_id, near_end) is not None


def test_signing_out_ends_the_session_and_is_idempotent(session, people):
    issued = sessions.start_session(session, people["ada"], T0)
    sessions.revoke(session, issued.token, T0)
    sessions.revoke(session, issued.token, T0)
    sessions.revoke(session, "unknown", T0)
    assert sessions.active_customer(session, people["ada"], issued.session_id, T0) is None
    with pytest.raises(UnauthorizedError):
        sessions.rotate(session, issued.token, T0)


def test_signing_out_everywhere_touches_only_your_sessions(session, people):
    phone = sessions.start_session(session, people["ada"], T0)
    laptop = sessions.start_session(session, people["ada"], T0)
    graces = sessions.start_session(session, people["grace"], T0)
    sessions.revoke_all(session, people["ada"], T0)
    assert sessions.active_customer(session, people["ada"], phone.session_id, T0) is None
    assert sessions.active_customer(session, people["ada"], laptop.session_id, T0) is None
    assert sessions.active_customer(session, people["grace"], graces.session_id, T0) is not None


def test_a_session_only_works_for_its_own_customer(session, people):
    issued = sessions.start_session(session, people["ada"], T0)
    assert sessions.active_customer(session, people["grace"], issued.session_id, T0) is None


# ---------------------------------------------------------------- the endpoints


class SessionDB:
    """The two Database calls the auth flow makes, on the test session."""

    def __init__(self, session):
        self.session = session

    def get_customer_auth_record(self, email):
        c = self.session.scalar(select(Customer).where(Customer.email == email))
        return {"id": c.id, "password_hash": c.password_hash} if c else None

    def get_session_customer(self, customer_id, session_id, now):
        c = sessions.active_customer(self.session, customer_id, session_id, now)
        return {"id": c.id, "name": c.name, "email": c.email, "plan": c.plan, "is_admin": c.is_admin} if c else None


@pytest.fixture
def client(session, people):
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.add_exception_handler(DomainError, domain_error_handler)
    app.state.db = SessionDB(session)
    app.dependency_overrides[deps.get_session] = lambda: session
    with TestClient(app) as c:
        yield c


def login(client):
    response = client.post("/auth/login", data={"username": "session-ada@example.test", "password": "ada-password"})
    assert response.status_code == 200
    return response


def me(client, access):
    return client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})


def test_login_returns_both_tokens_and_an_httponly_refresh_cookie(client):
    response = login(client)
    body = response.json()
    assert body["expires_in"] == 15 * 60 and body["refresh_expires_in"] > 6 * 24 * 3600
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/auth" in cookie
    assert me(client, body["access_token"]).json()["email"] == "session-ada@example.test"


def test_wrong_password_gets_no_session(client, session):
    response = client.post("/auth/login", data={"username": "session-ada@example.test", "password": "nope-nope"})
    assert response.status_code == 401 and "set-cookie" not in response.headers


def test_the_browser_refreshes_with_the_cookie_alone(client):
    first = login(client).json()
    refreshed = client.post("/auth/refresh")
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["refresh_token"] != first["refresh_token"]
    assert me(client, body["access_token"]).status_code == 200


def test_api_clients_refresh_with_the_token_in_the_body(client):
    first = login(client).json()
    client.cookies.clear()
    refreshed = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert refreshed.status_code == 200
    again = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert again.status_code == 401  # single use


def test_logging_out_kills_the_access_token_immediately(client):
    access = login(client).json()["access_token"]
    assert me(client, access).status_code == 200
    assert client.post("/auth/logout").status_code == 204
    assert me(client, access).status_code == 401  # not just when it expires
    assert client.post("/auth/refresh").status_code == 401


def test_logout_everywhere(client):
    first = login(client).json()
    client.cookies.clear()
    second = login(client).json()
    response = client.post("/auth/logout-all", headers={"Authorization": f"Bearer {second['access_token']}"})
    assert response.status_code == 204
    assert me(client, first["access_token"]).status_code == 401
    assert me(client, second["access_token"]).status_code == 401


def test_refresh_needs_a_token_and_rejects_unknown_fields(client):
    assert client.post("/auth/refresh").status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": "x", "customer_id": 1}).status_code == 422


def test_repeated_failed_sign_ins_lock_that_email_for_a_while(client):
    for _ in range(10):
        bad = client.post("/auth/login", data={"username": "session-ada@example.test", "password": "wrong-pass"})
        assert bad.status_code == 401
    locked = client.post("/auth/login", data={"username": "session-ada@example.test", "password": "ada-password"})
    assert locked.status_code == 429 and "Retry-After" in locked.headers  # even the right password waits
    # Any email is limited the same way, so a lock-out says nothing about who is registered.
    rate_limit.LOGIN_PER_IP.reset()  # so only the per-email rule is in play below
    for _ in range(10):
        client.post("/auth/login", data={"username": "nobody@example.test", "password": "wrong-pass"})
    assert client.post("/auth/login", data={"username": "nobody@example.test", "password": "x" * 8}).status_code == 429


def test_sign_in_attempts_are_limited_per_address(client):
    for _ in range(20):
        client.post("/auth/login", data={"username": "someone@example.test", "password": "wrong-pass"})
    assert login_status(client) == 429


def login_status(client):
    return client.post("/auth/login", data={"username": "session-ada@example.test", "password": "ada-password"}).status_code
