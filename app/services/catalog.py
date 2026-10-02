"""
app/services/catalog.py

Product-type rules: a product's `attributes` must match the attribute
definitions of its type (app/schemas/product_type.AttributeDef, stored as
plain dicts in product_types.attribute_schema).

  * every required attribute is present (and not blank)
  * no attribute the type doesn't define
  * each value has the declared kind; 'choice' values are one of the options

Pure functions -- no DB, no FastAPI -- used by the product and product-type
views and by seed_data.py.
"""

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
