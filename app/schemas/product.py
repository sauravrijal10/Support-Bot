from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.models.enums import BillingPeriod

SKU_PATTERN = r"^[A-Za-z0-9._-]+$"


class ProductCreate(BaseModel):
    # forbid: unknown fields are a 422, not silently ignored.
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64, pattern=SKU_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    billing_period: BillingPeriod = "one_time"
    stock_quantity: int | None = Field(
        default=None, ge=0, description="Units in stock. null = unlimited (digital goods)."
    )
    is_active: bool = True


class ProductUpdate(BaseModel):
    """Partial update: send only the fields you want to change."""

    model_config = ConfigDict(extra="forbid")

    sku: str | None = Field(default=None, min_length=1, max_length=64, pattern=SKU_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    billing_period: BillingPeriod | None = None
    # Explicit null is meaningful here (= unlimited), so it stays allowed.
    stock_quantity: int | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @model_validator(mode="after")
    def required_fields_cannot_be_null(self):
        for field in ("sku", "name", "description", "price", "billing_period", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    description: str
    price: Decimal
    billing_period: BillingPeriod
    stock_quantity: int | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def in_stock(self) -> bool:
        return self.stock_quantity is None or self.stock_quantity > 0
