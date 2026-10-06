from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.models.enums import BillingPeriod
from app.schemas.product_type import ProductTypeSummary

Attributes = dict[str, str | int | float | bool | None]
ATTRIBUTES_DESCRIPTION = (
    "Type-specific values, keyed by the attribute keys of the product type "
    "(see GET /product-types), e.g. {\"author\": \"...\", \"format\": \"paperback\"}."
)

SKU_PATTERN = r"^[A-Za-z0-9._-]+$"


class VariantIn(BaseModel):
    """One sellable version of a product, e.g. a size/colour."""

    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64, pattern=SKU_PATTERN)
    options: Attributes = Field(
        default_factory=dict,
        description="Values for the type's variant attributes, e.g. {\"size\": \"M\", \"color\": \"Black\"}.",
    )
    price: Decimal | None = Field(
        default=None, ge=0, max_digits=10, decimal_places=2, description="null = the product's price"
    )
    stock_quantity: int | None = Field(default=None, ge=0, description="null = unlimited")
    is_active: bool = True


class VariantOut(BaseModel):
    sku: str
    options: dict
    price: Decimal | None
    stock_quantity: int | None
    is_active: bool

    @computed_field
    @property
    def in_stock(self) -> bool:
        return self.is_active and (self.stock_quantity is None or self.stock_quantity > 0)


VARIANTS_DESCRIPTION = (
    "Sellable versions (sizes, colours...), stored as JSON on the product. When present, "
    "orders name a variant SKU, and stock is per variant (product stock_quantity must be null)."
)


class ProductCreate(BaseModel):
    # forbid: unknown fields are a 422, not silently ignored.
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64, pattern=SKU_PATTERN)
    product_type_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    attributes: Attributes = Field(default_factory=dict, description=ATTRIBUTES_DESCRIPTION)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    billing_period: BillingPeriod = "one_time"
    stock_quantity: int | None = Field(
        default=None, ge=0, description="Units in stock. null = unlimited (digital goods)."
    )
    variants: list[VariantIn] = Field(default_factory=list, max_length=50, description=VARIANTS_DESCRIPTION)
    is_active: bool = True


class ProductUpdate(BaseModel):
    """Partial update: send only the fields you want to change. `attributes`
    replaces the whole set. Changing the type usually needs new attributes
    in the same request."""

    model_config = ConfigDict(extra="forbid")

    sku: str | None = Field(default=None, min_length=1, max_length=64, pattern=SKU_PATTERN)
    product_type_id: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    attributes: Attributes | None = Field(default=None, description=ATTRIBUTES_DESCRIPTION)
    price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    billing_period: BillingPeriod | None = None
    # Explicit null is meaningful here (= unlimited), so it stays allowed.
    stock_quantity: int | None = Field(default=None, ge=0)
    variants: list[VariantIn] | None = Field(
        default=None, max_length=50, description=VARIANTS_DESCRIPTION + " Replaces the whole list."
    )
    is_active: bool | None = None

    @model_validator(mode="after")
    def required_fields_cannot_be_null(self):
        for field in (
            "sku", "product_type_id", "name", "description", "attributes",
            "price", "billing_period", "variants", "is_active",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    product_type: ProductTypeSummary
    name: str
    description: str
    attributes: dict
    price: Decimal
    billing_period: BillingPeriod
    stock_quantity: int | None
    variants: list[VariantOut]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def in_stock(self) -> bool:
        if self.variants:
            return any(v.in_stock for v in self.variants)
        return self.stock_quantity is None or self.stock_quantity > 0
