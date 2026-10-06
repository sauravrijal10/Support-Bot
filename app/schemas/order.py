from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import OrderStatus


class OrderLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(ge=1)
    variant_sku: str | None = Field(
        default=None, max_length=64,
        description="Required for products with variants (see the product's `variants`).",
    )
    quantity: int = Field(ge=1, le=100)


class OrderCreate(BaseModel):
    """Deliberately has no price, status, total or customer_id: those are set
    by the server (from the product table and the login token)."""

    model_config = ConfigDict(extra="forbid")

    items: list[OrderLineIn] = Field(min_length=1, max_length=20)


class OrderFormLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=100)


class OrderFormConfirm(BaseModel):
    """What the customer chose on the chat order form."""

    model_config = ConfigDict(extra="forbid")

    items: list[OrderFormLineIn] = Field(min_length=1, max_length=10)
    expected_total: Decimal = Field(
        ge=0, max_digits=12, decimal_places=2,
        description="The total the form showed. If current prices give a different total, "
        "nothing is ordered (409).",
    )


class OrderStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OrderStatus


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    variant_sku: str | None
    product_name: str
    unit_price: Decimal
    quantity: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    status: OrderStatus
    total_amount: Decimal
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemOut]
