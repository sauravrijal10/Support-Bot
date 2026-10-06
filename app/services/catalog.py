"""
app/services/catalog.py

Product-type rules: a product's `attributes` must match the attribute
definitions of its type (app/schemas/product_type.AttributeDef, stored as
plain dicts in product_types.attribute_schema).

  * every required attribute is present (and not blank)
  * no attribute the type doesn't define
  * each value has the declared kind; 'choice' values are one of the options

Attributes marked `"variant": true` (size, colour...) vary between a
product's VARIANTS. A product with variants sets them in each variant's
`options` and only the other attributes on itself; a product without
variants sets all of them on itself. See validate_product.

Pure functions -- no DB, no FastAPI -- used by the product and product-type
views and by seed_data.py.
"""

from decimal import Decimal

from app.services.errors import InvalidRequestError

MAX_TEXT_LENGTH = 500


def _check_value(definition: dict, value):
    """The value, normalised, or raise ValueError with the reason."""
    kind = definition.get("type", "text")

    if kind == "text":
        if not isinstance(value, str):
            raise ValueError("must be text")
        value = value.strip()
        if len(value) > MAX_TEXT_LENGTH:
            raise ValueError(f"must be at most {MAX_TEXT_LENGTH} characters")
        return value

    # bool is an int in Python; never accept true/false as a number.
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                return int(value)
            raise ValueError("must be a whole number")
        return value

    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        return value

    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value

    if kind == "choice":
        options = definition.get("options") or []
        if value not in options:
            raise ValueError(f"must be one of {options}")
        return value

    raise ValueError(f"has an unknown attribute type '{kind}'")


def attribute_errors(schema: list[dict], values: dict | None) -> list[str]:
    """Every problem with `values` under `schema` (empty list = valid)."""
    values = values or {}
    definitions = {d["key"]: d for d in schema}
    errors = [f"unknown attribute '{key}'" for key in values if key not in definitions]

    for key, definition in definitions.items():
        value = values.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if definition.get("required"):
                errors.append(f"'{key}' is required")
            continue
        try:
            _check_value(definition, value)
        except ValueError as reason:
            errors.append(f"'{key}' {reason}")
    return errors


def validate_attributes(schema: list[dict], values: dict | None, type_name: str = "this type") -> dict:
    """Normalised attributes (text trimmed, empty optional values dropped),
    or InvalidRequestError (422) listing every problem."""
    errors = attribute_errors(schema, values)
    if errors:
        raise InvalidRequestError(f"Invalid attributes for {type_name}: " + "; ".join(errors))

    cleaned = {}
    definitions = {d["key"]: d for d in schema}
    for key, value in (values or {}).items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        cleaned[key] = _check_value(definitions[key], value)
    return cleaned


def describe_attributes(schema: list[dict], values: dict | None) -> dict:
    """Attributes keyed by their human label, with units, for showing to the
    chat model: {"Storage": "500 GB", "Format": "paperback"}."""
    values = values or {}
    shown = {}
    for definition in schema:
        if definition["key"] not in values:
            continue
        value = values[definition["key"]]
        if isinstance(value, bool):
            value = "yes" if value else "no"
        unit = definition.get("unit")
        shown[definition.get("label") or definition["key"]] = f"{value} {unit}" if unit else value
    return shown


MAX_VARIANTS = 50


def split_schema(schema: list[dict]) -> tuple[list[dict], list[dict]]:
    """(product-level definitions, variant-level definitions)."""
    return ([d for d in schema if not d.get("variant")], [d for d in schema if d.get("variant")])


def variant_errors(
    schema: list[dict], attributes: dict | None, variants: list[dict], product_sku: str = ""
) -> list[str]:
    """Every problem with a product's attributes + variants under `schema`.
    `variants` are dicts with at least "sku" and "options"."""
    if not variants:
        return attribute_errors(schema, attributes)

    product_defs, variant_defs = split_schema(schema)
    if not variant_defs:
        return ["this product type has no variant attributes (mark e.g. size or colour as "
                "\"variant\": true on the type first)"]
    variant_keys = {d["key"] for d in variant_defs}
    attributes = attributes or {}
    errors = [f"'{k}' varies per variant: set it in each variant's options"
              for k in attributes if k in variant_keys]
    errors += attribute_errors(product_defs, {k: v for k, v in attributes.items() if k not in variant_keys})
    if len(variants) > MAX_VARIANTS:
        errors.append(f"at most {MAX_VARIANTS} variants")

    seen_skus, seen_options = {product_sku.upper()}, set()
    for v in variants:
        sku = v["sku"].upper()
        if sku in seen_skus:
            errors.append(f"variant SKU '{sku}' is used twice (or equals the product SKU)")
        seen_skus.add(sku)
        errors += [f"variant {sku}: {e}" for e in attribute_errors(variant_defs, v.get("options"))]
        options = v.get("options") or {}
        combo = tuple(sorted((k, str(val)) for k, val in options.items() if val is not None))
        if combo in seen_options:
            errors.append(f"variant {sku}: another variant has the same options")
        seen_options.add(combo)
    return errors


def validate_product(
    schema: list[dict], attributes: dict | None, variants: list[dict], product_sku: str,
    type_name: str = "this type",
) -> tuple[dict, list[dict]]:
    """(clean attributes, clean variants) or InvalidRequestError (422)
    listing every problem. Variants come back in their stored JSON form:
    {"sku" (upper-case), "options", "price" (string or null),
     "stock_quantity", "is_active"}."""
    errors = variant_errors(schema, attributes, variants, product_sku)
    if errors:
        raise InvalidRequestError(f"Invalid product for {type_name}: " + "; ".join(errors))
    if not variants:
        return validate_attributes(schema, attributes, type_name), []

    product_defs, variant_defs = split_schema(schema)
    clean = []
    for v in variants:
        price = v.get("price")
        clean.append({
            "sku": v["sku"].upper(),
            "options": validate_attributes(variant_defs, v.get("options"), type_name),
            "price": None if price is None else f"{Decimal(str(price)):.2f}",
            "stock_quantity": v.get("stock_quantity"),
            "is_active": v.get("is_active", True),
        })
    return validate_attributes(product_defs, attributes, type_name), clean

