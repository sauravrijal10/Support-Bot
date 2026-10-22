from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    # forbid: a request smuggling in e.g. `is_admin` is rejected, not ignored.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    # mode="before" so trimming/lowercasing happens ahead of the pattern
    # check, instead of rejecting " User@Example.com " outright.
    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class CustomerProfile(BaseModel):
    id: int
    name: str
    email: str
    plan: str
    is_admin: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Access token lifetime in seconds (short)")
    refresh_token: str = Field(
        ...,
        description="Exchange at POST /auth/refresh for a new access token. Single use: each "
        "refresh returns a new one. Browsers also get it as an HttpOnly cookie.",
    )
    refresh_expires_in: int = Field(..., description="Refresh token lifetime in seconds")


class RefreshRequest(BaseModel):
    """Optional body for /auth/refresh and /auth/logout. Browsers can send
    nothing: the HttpOnly refresh cookie is used instead."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str | None = Field(default=None, min_length=1, max_length=200)
