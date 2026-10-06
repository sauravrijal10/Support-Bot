"""
Tests for product types: attribute definitions (schemas) and checking a
product's attributes against its type (app/services/catalog.py). Pure
functions, so no database is needed.
"""

import pytest
from pydantic import ValidationError

from app.schemas.product import ProductUpdate
from app.schemas.product_type import ProductTypeCreate, ProductTypeUpdate
from app.services.catalog import attribute_errors, describe_attributes, validate_attributes
from app.services.errors import InvalidRequestError

CLOTHING = [
    {"key": "size", "label": "Size", "type": "choice", "required": True, "options": ["S", "M", "L"]},
    {"key": "color", "label": "Colour", "type": "text", "required": True},
    {"key": "weight_g", "label": "Weight", "type": "integer", "unit": "g"},
    {"key": "rating", "label": "Rating", "type": "number"},
    {"key": "organic", "label": "Organic", "type": "boolean"},
]


# ---------------------------------------------------------------- product attributes


def test_valid_attributes_are_normalised():
    cleaned = validate_attributes(
        CLOTHING, {"size": "M", "color": "  Black ", "weight_g": 180.0, "rating": 4.5, "organic": False}
    )
    assert cleaned == {"size": "M", "color": "Black", "weight_g": 180, "rating": 4.5, "organic": False}
    assert isinstance(cleaned["weight_g"], int)


def test_empty_optional_values_are_dropped():
    assert validate_attributes(CLOTHING, {"size": "S", "color": "Red", "weight_g": None, "rating": None}) == {
        "size": "S", "color": "Red",
    }


@pytest.mark.parametrize(
    "values, problem",
    [
        ({"color": "Black"}, "'size' is required"),
        ({"size": "M", "color": "   "}, "'color' is required"),
        ({"size": "XXL", "color": "Black"}, "'size' must be one of"),
        ({"size": "M", "color": "Black", "sleeve": "long"}, "unknown attribute 'sleeve'"),
        ({"size": "M", "color": 7}, "'color' must be text"),
        ({"size": "M", "color": "Black", "weight_g": 180.5}, "'weight_g' must be a whole number"),
        ({"size": "M", "color": "Black", "weight_g": "180"}, "'weight_g' must be a whole number"),
        ({"size": "M", "color": "Black", "weight_g": True}, "'weight_g' must be a whole number"),
        ({"size": "M", "color": "Black", "rating": True}, "'rating' must be a number"),
        ({"size": "M", "color": "Black", "organic": "yes"}, "'organic' must be true or false"),
        ({"size": "M", "color": "x" * 501}, "'color' must be at most 500 characters"),
    ],
)
def test_invalid_attributes_are_rejected(values, problem):
    with pytest.raises(InvalidRequestError) as caught:
        validate_attributes(CLOTHING, values, "Clothing")
    assert caught.value.status_code == 422
    assert problem in caught.value.detail and "Clothing" in caught.value.detail


def test_every_problem_is_reported_at_once():
    errors = attribute_errors(CLOTHING, {"size": "XXL", "fabric": "wool"})
    assert len(errors) == 3  # unknown 'fabric', bad 'size', missing 'color'


def test_a_type_without_attributes_accepts_only_none():
    assert validate_attributes([], {}) == {} and validate_attributes([], None) == {}
    with pytest.raises(InvalidRequestError):
        validate_attributes([], {"anything": "x"})


def test_describe_uses_labels_units_and_schema_order():
    shown = describe_attributes(CLOTHING, {"organic": True, "weight_g": 180, "size": "M"})
    assert shown == {"Size": "M", "Weight": "180 g", "Organic": "yes"}
    assert list(shown) == ["Size", "Weight", "Organic"]


# ---------------------------------------------------------------- type definitions


def test_a_valid_type_definition():
    t = ProductTypeCreate(slug="clothing", name="Clothing", attribute_schema=CLOTHING)
    assert [a.key for a in t.attribute_schema] == ["size", "color", "weight_g", "rating", "organic"]


@pytest.mark.parametrize(
    "schema, problem",
    [
        ([{"key": "size", "label": "Size", "type": "choice"}], "needs options"),
        ([{"key": "size", "label": "Size", "type": "choice", "options": ["S", "S"]}], "unique"),
        ([{"key": "size", "label": "Size", "options": ["S"]}], "only allowed for type 'choice'"),
        ([{"key": "a", "label": "A"}, {"key": "a", "label": "Again"}], "duplicate attribute keys"),
        ([{"key": "Size", "label": "Size"}], "pattern"),
        ([{"key": "size", "label": "Size", "type": "date"}], "type"),
        ([{"key": "size", "label": "Size", "colour": "red"}], "Extra inputs"),
    ],
)
def test_invalid_type_definitions_are_rejected(schema, problem):
    with pytest.raises(ValidationError) as caught:
        ProductTypeCreate(slug="x", name="X", attribute_schema=schema)
    assert problem in str(caught.value)


@pytest.mark.parametrize("slug", ["Books", "home kitchen", "-books", "books-", "home--kitchen", ""])
def test_slugs_must_be_lowercase_words_joined_by_hyphens(slug):
    with pytest.raises(ValidationError):
        ProductTypeCreate(slug=slug, name="X")


def test_partial_updates_cannot_null_required_fields():
    with pytest.raises(ValidationError):
        ProductTypeUpdate(name=None)
    with pytest.raises(ValidationError):
        ProductUpdate(product_type_id=None)
    with pytest.raises(ValidationError):
        ProductUpdate(attributes=None)
    assert ProductUpdate(stock_quantity=None).model_fields_set == {"stock_quantity"}


def test_product_attribute_values_keep_their_json_types():
    update = ProductUpdate(attributes={"pages": 312, "wireless": True, "rating": 4.5, "author": "Maya"})
    assert update.attributes == {"pages": 312, "wireless": True, "rating": 4.5, "author": "Maya"}
    assert type(update.attributes["wireless"]) is bool and type(update.attributes["pages"]) is int


# ---------------------------------------------------------------- variants

from app.services.catalog import validate_product, variant_errors  # noqa: E402

SHIRTS = [
    {"key": "size", "label": "Size", "type": "choice", "required": True, "variant": True, "options": ["M", "L"]},
    {"key": "color", "label": "Colour", "type": "text", "required": True, "variant": True},
    {"key": "material", "label": "Material", "type": "text"},
]


def _variant(sku, **options):
    return {"sku": sku, "options": options, "price": None, "stock_quantity": 3, "is_active": True}


def test_variants_are_validated_and_normalised():
    attrs, variants = validate_product(
        SHIRTS, {"material": "Cotton"},
        [_variant("shirt-blk-m", size="M", color=" Black "), {**_variant("SHIRT-BLK-L", size="L", color="Black"), "price": 26}],
        "SHIRT",
    )
    assert attrs == {"material": "Cotton"}
    assert variants[0] == {"sku": "SHIRT-BLK-M", "options": {"size": "M", "color": "Black"},
                           "price": None, "stock_quantity": 3, "is_active": True}
    assert variants[1]["price"] == "26.00"


def test_without_variants_every_attribute_lives_on_the_product():
    attrs, variants = validate_product(SHIRTS, {"size": "M", "color": "Red"}, [], "SHIRT")
    assert variants == [] and attrs == {"size": "M", "color": "Red"}
    with pytest.raises(InvalidRequestError, match="'size' is required"):
        validate_product(SHIRTS, {"color": "Red"}, [], "SHIRT")


@pytest.mark.parametrize(
    "attributes, variants, problem",
    [
        ({"size": "M"}, [_variant("A", size="L", color="Red")], "'size' varies per variant"),
        ({}, [_variant("A", size="M")], "variant A: 'color' is required"),
        ({}, [_variant("A", size="XL", color="Red")], "variant A: 'size' must be one of"),
        ({}, [_variant("A", size="M", color="Red", fabric="silk")], "unknown attribute 'fabric'"),
        ({}, [_variant("A", size="M", color="Red"), _variant("a", size="L", color="Red")], "used twice"),
        ({}, [_variant("SHIRT", size="M", color="Red")], "equals the product SKU"),
        ({}, [_variant("A", size="M", color="Red"), _variant("B", size="M", color="Red")], "same options"),
    ],
)
def test_invalid_variants_are_rejected(attributes, variants, problem):
    assert any(problem in e for e in variant_errors(SHIRTS, attributes, variants, "SHIRT"))
    with pytest.raises(InvalidRequestError):
        validate_product(SHIRTS, attributes, variants, "SHIRT")


def test_a_type_without_variant_attributes_cannot_have_variants():
    errors = variant_errors(CLOTHING, {}, [_variant("A", size="M")], "X")
    assert errors and "no variant attributes" in errors[0]
