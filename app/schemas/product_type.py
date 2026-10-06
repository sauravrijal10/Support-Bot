from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
ATTRIBUTE_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
MAX_ATTRIBUTES = 30

AttributeKind = Literal["text", "integer", "number", "boolean", "choice"]


class AttributeDef(BaseModel):
    """One attribute that products of a type carry, e.g.
    {"key": "size", "label": "Size", "type": "choice", "required": true,
     "options": ["S", "M", "L"]}"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=ATTRIBUTE_KEY_PATTERN, description="snake_case, used in product.attributes")
    label: str = Field(min_length=1, max_length=60)
    type: AttributeKind = "text"
    required: bool = False
    options: list[str] | None = Field(
        default=None, min_length=1, max_length=50, description="Allowed values; only for type 'choice'."
    )
    unit: str | None = Field(default=None, max_length=20, description="e.g. 'GB' or 'months'")
    variant: bool = Field(
        default=False,
        description="Varies between a product's variants (e.g. size, colour). Products with "
        "variants set it in each variant's options instead of on the product.",
    )

    @model_validator(mode="after")
    def options_only_for_choice(self):
        if self.type == "choice":
            if not self.options:
                raise ValueError(f"attribute '{self.key}': a 'choice' attribute needs options")
            if len(set(self.options)) != len(self.options):
                raise ValueError(f"attribute '{self.key}': options must be unique")
        elif self.options is not None:
            raise ValueError(f"attribute '{self.key}': options are only allowed for type 'choice'")
        return self


def _unique_keys(schema: list[AttributeDef] | None) -> None:
    if schema is None:
        return
    keys = [a.key for a in schema]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"duplicate attribute keys: {duplicates}")


class ProductTypeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    attribute_schema: list[AttributeDef] = Field(default_factory=list, max_length=MAX_ATTRIBUTES)
    is_active: bool = True

    @model_validator(mode="after")
    def keys_are_unique(self):
        _unique_keys(self.attribute_schema)
        return self


class ProductTypeUpdate(BaseModel):
    """Partial update. Changing attribute_schema is refused if any existing
    product of this type would no longer be valid under it."""

    model_config = ConfigDict(extra="forbid")

    slug: str | None = Field(default=None, min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    attribute_schema: list[AttributeDef] | None = Field(default=None, max_length=MAX_ATTRIBUTES)
    is_active: bool | None = None

    @model_validator(mode="after")
    def check(self):
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        _unique_keys(self.attribute_schema)
        return self


class ProductTypeSummary(BaseModel):
    """How a type is shown inside a product."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str


class ProductTypeOut(ProductTypeSummary):
    description: str
    attribute_schema: list[AttributeDef]
    is_active: bool
    created_at: datetime
    updated_at: datetime
