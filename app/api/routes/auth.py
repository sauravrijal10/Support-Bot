"""
app/api/routes/auth.py

Registration, sign-in and sign-out.

Sign-in returns a short-lived access token (send it as `Authorization:
Bearer`) and a refresh token that renews it (app/services/sessions.py has the
rules: single use, rotation, reuse detection). The refresh token is returned
in the body for API clients and also set as an HttpOnly cookie, scoped to
/auth and SameSite=Strict, so the browser chat page never handles it in
JavaScript and other sites can't make the browser use it.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.deps import client_ip, get_current_customer, get_session
from app.schemas.auth import CustomerProfile, RefreshRequest, RegisterRequest, TokenResponse
from app.services import rate_limit, sessions
from app.services.auth import create_access_token, hash_password, verify_password
from app.services.errors import UnauthorizedError

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokens(issued: sessions.IssuedRefresh, request: Request, response: Response) -> TokenResponse:
    """The access + refresh token pair, with the refresh cookie set."""
    access, expires_in = create_access_token(issued.customer_id, issued.session_id)
    refresh_expires_in = max(0, int((issued.expires_at - _now()).total_seconds()))
    response.set_cookie(
        REFRESH_COOKIE, issued.token, max_age=refresh_expires_in, path="/auth",
        httponly=True, samesite="strict", secure=request.url.scheme == "https",
    )
    return TokenResponse(
        access_token=access, expires_in=expires_in,
        refresh_token=issued.token, refresh_expires_in=refresh_expires_in,
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/auth", httponly=True, samesite="strict")


def _refresh_token(request: Request, payload: RefreshRequest | None) -> str | None:
    return (payload.refresh_token if payload else None) or request.cookies.get(REFRESH_COOKIE)


@router.post(
    "/register", response_model=CustomerProfile, status_code=status.HTTP_201_CREATED
)
def register(payload: RegisterRequest, request: Request) -> CustomerProfile:
    rate_limit.take(
        [(rate_limit.REGISTER_PER_IP, client_ip(request))], "Too many accounts created from here."
    )
    customer = request.app.state.db.create_customer(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
    )
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )
    return CustomerProfile(**customer)


@router.post("/login", response_model=TokenResponse)
def login(
    request: Request,
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> TokenResponse:
    """OAuth2 password flow: send form fields `username` (your email) and
    `password`. Using the standard form is what makes the Authorize button
    in Swagger UI work. Starts a new sign-in session."""
    email = form.username.strip().lower()
    rate_limit.take([(rate_limit.LOGIN_PER_IP, client_ip(request))], "Too many sign-in attempts.")
    # Counted per email whether or not it is registered, so a lock-out
    # reveals nothing about which emails have accounts.
    rate_limit.check(
        [(rate_limit.LOGIN_FAILURES_PER_EMAIL, email)],
        "Too many failed sign-in attempts for this account.",
    )
    record = request.app.state.db.get_customer_auth_record(email)
    password_hash = record["password_hash"] if record else None

    # verify_password does equivalent work for unknown users, and the
    # message is identical for every failure, so neither timing nor the
    # error text reveals whether an email is registered.
    if not verify_password(form.password, password_hash):
        rate_limit.LOGIN_FAILURES_PER_EMAIL.hit(email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    issued = sessions.start_session(session, record["id"], _now())
    session.commit()
    return _tokens(issued, request, response)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = Body(default=None),
    session: Session = Depends(get_session),
) -> TokenResponse:
    """A new access token and a new refresh token, for the refresh token in
    the body or the cookie. The one sent is used up: send it again and you
    get 401 -- and, after a short grace period, the whole session is signed
    out, because a reused token means someone else may have a copy."""
    rate_limit.take([(rate_limit.REFRESH_PER_IP, client_ip(request))], "Too many token refreshes.")
    token = _refresh_token(request, payload)
    if not token:
        raise UnauthorizedError(sessions.INVALID_REFRESH)
    try:
        issued = sessions.rotate(session, token, _now())
    except UnauthorizedError:
        session.commit()  # keep a reuse-triggered revocation
        raise
    session.commit()
    return _tokens(issued, request, response)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    payload: RefreshRequest | None = Body(default=None),
    session: Session = Depends(get_session),
) -> Response:
    """Sign this session out: its refresh token and access tokens stop
    working immediately. Always 204, even for an unknown token."""
    token = _refresh_token(request, payload)
    if token:
        sessions.revoke(session, token, _now())
        session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_cookie(response)
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(
    customer: dict = Depends(get_current_customer),
    session: Session = Depends(get_session),
) -> Response:
    """Sign out every session of yours, on every device (e.g. after losing a
    phone). Needs a valid access token."""
    sessions.revoke_all(session, customer["id"], _now())
    session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_cookie(response)
    return response


@router.get("/me", response_model=CustomerProfile)
def me(customer: dict = Depends(get_current_customer)) -> CustomerProfile:
    return CustomerProfile(**customer)
