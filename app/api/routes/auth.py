from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import get_current_customer
from app.schemas.auth import CustomerProfile, RegisterRequest, TokenResponse
from app.services.auth import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=CustomerProfile, status_code=status.HTTP_201_CREATED
)
def register(payload: RegisterRequest, request: Request) -> CustomerProfile:
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
    request: Request, form: OAuth2PasswordRequestForm = Depends()
) -> TokenResponse:
    """OAuth2 password flow: send form fields `username` (your email) and
    `password`. Using the standard form is what makes the Authorize button
    in Swagger UI work."""
    record = request.app.state.db.get_customer_auth_record(
        form.username.strip().lower()
    )
    password_hash = record["password_hash"] if record else None

    # verify_password does equivalent work for unknown users, and the
    # message is identical for every failure, so neither timing nor the
    # error text reveals whether an email is registered.
    if not verify_password(form.password, password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, expires_in = create_access_token(record["id"])
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=CustomerProfile)
def me(customer: dict = Depends(get_current_customer)) -> CustomerProfile:
    return CustomerProfile(**customer)
