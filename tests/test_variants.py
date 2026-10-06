"""
Variants stored in products.variants (JSONB): ordering, stock, cancelling,
SKU lookup and uniqueness. Runs against Postgres (DATABASE_URL) inside a
rolled-back transaction; skipped without a database.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Customer, Product, ProductType
from app.services import variants
from app.services.errors import ConflictError, InvalidRequestError
from app.services.orders import create_order, transition_order


@pytest.fixture
def shop(session):
    kind = ProductType(slug="test-kind-variants", name="Test kind", attribute_schema=[
        {"key": "size", "label": "Size", "type": "text", "variant": True},
    ])
    customer = Customer(name="Variant Tester", email="variant-tester@example.test")
    shirt = Product(sku="TV-SHIRT", name="Shirt", price=Decimal("20.00"), product_type=kind, variants=[
        {"sku": "TV-SHIRT-M", "options": {"size": "M"}, "price": None, "stock_quantity": 4, "is_active": True},
        {"sku": "TV-SHIRT-L", "options": {"size": "L"}, "price": "22.50", "stock_quantity": 0, "is_active": True},
        {"sku": "TV-SHIRT-XL", "options": {"size": "XL"}, "price": None, "stock_quantity": None, "is_active": False},
    ])
    mug = Product(sku="TV-MUG", name="Mug", price=Decimal("9.00"), stock_quantity=2, product_type=kind)
    session.add_all([kind, customer, shirt, mug])
    session.flush()
    return {"customer": customer.id, "shirt": shirt, "mug": mug}


def stock(product):
    return {v["sku"]: v["stock_quantity"] for v in product.variants}


def test_ordering_a_variant_uses_its_price_and_stock(session, shop):
    order = create_order(session, shop["customer"], [(shop["shirt"].id, "tv-shirt-m", 3), (shop["mug"].id, None, 1)])
    assert order.total_amount == Decimal("69.00")
    shirt_item = next(i for i in order.items if i.variant_sku)
    assert (shirt_item.variant_sku, shirt_item.product_name, shirt_item.unit_price) == (
        "TV-SHIRT-M", "Shirt (Size: M)", Decimal("20.00"))
    assert stock(shop["shirt"])["TV-SHIRT-M"] == 1 and shop["mug"].stock_quantity == 1


def test_the_variant_price_overrides_the_product_price(session, shop):
    shop["shirt"].variants = [{**v, "stock_quantity": 5} if v["sku"] == "TV-SHIRT-L" else v
                              for v in shop["shirt"].variants]
    session.flush()
    order = create_order(session, shop["customer"], [(shop["shirt"].id, "TV-SHIRT-L", 2)])
    assert order.total_amount == Decimal("45.00")


@pytest.mark.parametrize(
    "variant_sku, error, message",
    [(None, InvalidRequestError, "Choose a variant"), ("TV-SHIRT-XL", InvalidRequestError, "not available"),
     ("TV-SHIRT-NOPE", InvalidRequestError, "not available"), ("TV-SHIRT-L", ConflictError, "Not enough stock")],
)
def test_bad_variant_orders_are_refused(session, shop, variant_sku, error, message):
    with pytest.raises(error, match=message):
        create_order(session, shop["customer"], [(shop["shirt"].id, variant_sku, 1)])


def test_a_variant_cannot_be_ordered_on_a_product_without_variants(session, shop):
    with pytest.raises(InvalidRequestError):
        create_order(session, shop["customer"], [(shop["mug"].id, "TV-SHIRT-M", 1)])


def test_cancelling_returns_stock_to_the_variant(session, shop):
    order = create_order(session, shop["customer"], [(shop["shirt"].id, "TV-SHIRT-M", 4)])
    assert stock(shop["shirt"])["TV-SHIRT-M"] == 0
    transition_order(session, order, "cancelled", is_admin=False)
    assert stock(shop["shirt"]) == {"TV-SHIRT-M": 4, "TV-SHIRT-L": 0, "TV-SHIRT-XL": None}


def test_stock_changes_are_saved(session, shop):
    create_order(session, shop["customer"], [(shop["shirt"].id, "TV-SHIRT-M", 1)])
    session.flush()
    session.expire(shop["shirt"])  # re-read from the database
    assert stock(shop["shirt"])["TV-SHIRT-M"] == 3


def test_resolving_skus(session, shop):
    assert variants.resolve_sku(session, "tv-shirt-l") == (shop["shirt"], shop["shirt"].variants[1])
    assert variants.resolve_sku(session, "TV-SHIRT") == (shop["shirt"], None)
    assert variants.resolve_sku(session, "TV-MUG") == (shop["mug"], None)
    assert variants.resolve_sku(session, "TV-NOPE") is None


def test_skus_are_unique_across_products_and_variants(session, shop):
    assert variants.sku_taken(session, "tv-shirt-m")  # another product's variant
    assert variants.sku_taken(session, "TV-MUG")
    assert not variants.sku_taken(session, "TV-SHIRT-M", exclude_product_id=shop["shirt"].id)
    assert not variants.sku_taken(session, "TV-NEW")


def test_in_stock_looks_at_variants(session, shop):
    def in_stock():
        return set(session.scalars(
            select(Product.sku).where(Product.sku.like("TV-%"), variants.in_stock_clause())
        ))

    assert in_stock() == {"TV-SHIRT", "TV-MUG"}
    shop["shirt"].variants = [{**v, "stock_quantity": 0} for v in shop["shirt"].variants[:2]] + [
        shop["shirt"].variants[2]]  # XL has unlimited stock but is inactive
    shop["mug"].stock_quantity = 0
    session.flush()
    assert in_stock() == set()
