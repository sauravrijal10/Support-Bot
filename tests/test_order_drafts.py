"""
Tests for ordering through the bot (app/services/order_drafts.py).

These need Postgres (row locks, savepoints, JSONB), so they run against
DATABASE_URL and are SKIPPED when it isn't set or reachable. Each test runs
inside a transaction that is rolled back afterwards, so no data is left
behind. With docker compose:

    docker compose run --rm app sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Customer, Order, OrderDraft, Product, ProductType
from app.services.errors import ConflictError, InvalidRequestError, NotFoundError
from app.services.order_drafts import (
    DRAFT_LIFETIME,
    confirm_draft,
    confirm_form,
    discard_form,
    is_confirmation,
    prepare_cart,
    prepare_draft,
)

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
LATER = T0 + timedelta(minutes=1)  # the customer's next message


# ---------------------------------------------------------------- confirmation wording (no DB)


@pytest.mark.parametrize(
    "message", ["yes", "Yes please", "ok", "Sure, go ahead", "confirm", "Place the order", "yep that's right"]
)
def test_clear_yes(message):
    assert is_confirmation(message)


@pytest.mark.parametrize(
    "message",
    ["", "no", "not yet", "don't place it", "wait", "yes but change the size to L", "cancel",
     "how much is shipping?", "booking", "I'll think about it"],
)
def test_not_a_yes(message):
    assert not is_confirmation(message)


# ---------------------------------------------------------------- with Postgres


@pytest.fixture
def shop(session):
    kind = ProductType(slug="test-kind-drafts", name="Test kind", attribute_schema=[
        {"key": "size", "label": "Size", "type": "text", "variant": True},
    ])
    customer = Customer(name="Draft Tester", email="draft-tester@example.test")
    other = Customer(name="Other", email="draft-other@example.test")
    tee = Product(sku="TEST-TEE", name="Test tee", price=Decimal("24.00"), stock_quantity=5, product_type=kind)
    ebook = Product(sku="TEST-EBOOK", name="Test e-book", price=Decimal("9.99"), stock_quantity=None, product_type=kind)
    shirt = Product(sku="TEST-SHIRT", name="Test shirt", price=Decimal("24.00"), product_type=kind, variants=[
        {"sku": "TEST-SHIRT-M", "options": {"size": "M"}, "price": None, "stock_quantity": 5, "is_active": True},
        {"sku": "TEST-SHIRT-L", "options": {"size": "L"}, "price": "26.00", "stock_quantity": 0, "is_active": True},
        {"sku": "TEST-SHIRT-XL", "options": {"size": "XL"}, "price": None, "stock_quantity": 9, "is_active": False},
    ])
    gone = Product(sku="TEST-GONE", name="Withdrawn", price=Decimal("1.00"), is_active=False, product_type=kind)
    session.add_all([kind, customer, other, tee, shirt, ebook, gone])
    session.flush()
    return {"customer": customer.id, "other": other.id, "tee": tee, "ebook": ebook, "shirt": shirt}


def drafts(session, customer_id):
    return session.scalars(
        select(OrderDraft).where(OrderDraft.customer_id == customer_id).order_by(OrderDraft.id)
    ).all()


def test_prepare_quotes_and_places_nothing(session, shop):
    result = prepare_draft(session, shop["customer"], [("test-tee", 2), ("TEST-EBOOK", 1)], T0)
    assert result["draft_ready"] and result["total"] == "57.99"
    assert [i["line_total"] for i in result["items"]] == ["48.00", "9.99"]
    assert shop["tee"].stock_quantity == 5  # nothing reserved
    assert session.scalar(select(Order.id).where(Order.customer_id == shop["customer"])) is None
    [draft] = drafts(session, shop["customer"])
    assert draft.status == "open" and draft.expires_at == T0 + DRAFT_LIFETIME


def test_duplicate_skus_are_merged(session, shop):
    result = prepare_draft(session, shop["customer"], [("TEST-TEE", 1), ("test-tee", 2)], T0)
    assert [(i["sku"], i["quantity"]) for i in result["items"]] == [("TEST-TEE", 3)]


@pytest.mark.parametrize(
    "lines, problem",
    [([("NOPE", 1)], "No product"), ([("TEST-GONE", 1)], "No product"), ([("TEST-TEE", 6)], "Not enough stock")],
)
def test_prepare_refuses_unknown_withdrawn_or_short_stock(session, shop, lines, problem):
    result = prepare_draft(session, shop["customer"], lines, T0)
    assert problem in result["error"] and drafts(session, shop["customer"]) == []


def test_confirm_in_a_later_message_places_the_order(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-TEE", 2)], T0)
    result = confirm_draft(session, shop["customer"], request_started_at=LATER, now=LATER)

    assert result["placed"] and result["order"]["total"] == "48.00"
    order = session.get(Order, result["order"]["order_id"])
    assert order.customer_id == shop["customer"] and order.status == "pending"
    assert shop["tee"].stock_quantity == 3
    [draft] = drafts(session, shop["customer"])
    assert draft.status == "placed" and draft.order_id == order.id


def test_confirm_in_the_same_request_is_refused(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], LATER)
    result = confirm_draft(session, shop["customer"], request_started_at=T0, now=LATER)
    assert result["placed"] is False and "hasn't seen" in result["reason"]
    assert shop["tee"].stock_quantity == 5


def test_a_draft_is_placed_only_once(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)
    assert confirm_draft(session, shop["customer"], LATER, LATER)["placed"]
    second = confirm_draft(session, shop["customer"], LATER, LATER)
    assert second["placed"] is False and "no order waiting" in second["reason"]
    assert shop["tee"].stock_quantity == 4


def test_a_new_quote_replaces_the_old_one(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)
    prepare_draft(session, shop["customer"], [("TEST-EBOOK", 1)], T0)
    result = confirm_draft(session, shop["customer"], LATER, LATER)
    assert [i["product"] for i in result["order"]["items"]] == ["Test e-book"]
    assert [d.status for d in drafts(session, shop["customer"])] == ["superseded", "placed"]


def test_expired_quotes_are_not_placed(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)
    late = T0 + DRAFT_LIFETIME + timedelta(seconds=1)
    result = confirm_draft(session, shop["customer"], late, late)
    assert result["placed"] is False and "expired" in result["reason"]
    assert drafts(session, shop["customer"])[0].status == "expired"


def test_a_price_change_since_the_quote_blocks_the_order_and_restores_stock(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-TEE", 2)], T0)
    shop["tee"].price = Decimal("30.00")
    session.flush()

    result = confirm_draft(session, shop["customer"], LATER, LATER)
    assert result["placed"] is False and "Prices changed" in result["reason"]
    assert shop["tee"].stock_quantity == 5  # the savepoint undid the decrement
    assert session.scalar(select(Order.id).where(Order.customer_id == shop["customer"])) is None


def test_stock_running_out_after_the_quote_is_reported(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-TEE", 5)], T0)
    shop["tee"].stock_quantity = 1
    session.flush()

    result = confirm_draft(session, shop["customer"], LATER, LATER)
    assert result["placed"] is False and "Not enough stock" in result["reason"]
    assert shop["tee"].stock_quantity == 1


def test_customers_cannot_confirm_each_others_quotes(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)
    result = confirm_draft(session, shop["other"], LATER, LATER)
    assert result["placed"] is False and shop["tee"].stock_quantity == 5


# ---------------------------------------------------------------- the web order form


def test_the_quote_includes_a_form_with_the_products_variants(session, shop):
    result = prepare_draft(session, shop["customer"], [("test-shirt-m", 2)], T0)
    form = result["form"]
    assert form["draft_id"] == drafts(session, shop["customer"])[0].id and form["total"] == "48.00"
    [line] = form["lines"]
    assert line["sku"] == "TEST-SHIRT-M" and line["quantity"] == 2
    # The product's active variants, with their own price and stock (XL is inactive).
    assert [(o["sku"], o["name"], o["unit_price"], o["max_quantity"]) for o in line["options"]] == [
        ("TEST-SHIRT-M", "Test shirt (Size: M)", "24.00", 5),
        ("TEST-SHIRT-L", "Test shirt (Size: L)", "26.00", 0),
    ]


def test_a_product_without_variants_is_its_own_only_option(session, shop):
    form = prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)["form"]
    assert [o["sku"] for o in form["lines"][0]["options"]] == ["TEST-TEE"]


def test_naming_the_base_product_preselects_an_in_stock_variant(session, shop):
    result = prepare_draft(session, shop["customer"], [("TEST-SHIRT", 1)], T0)
    assert result["form"]["lines"][0]["sku"] == "TEST-SHIRT-M"
    assert result["preselected_variants"] == ["Test shirt (Size: M)"]


@pytest.mark.parametrize("sku", ["TEST-SHIRT-XL", "TEST-SHIRT-NOPE"])
def test_inactive_or_unknown_variants_cannot_be_quoted(session, shop, sku):
    assert "No product" in prepare_draft(session, shop["customer"], [(sku, 1)], T0)["error"]


def test_variant_stock_is_checked_when_quoting(session, shop):
    result = prepare_draft(session, shop["customer"], [("TEST-SHIRT-M", 6)], T0)
    assert result["unavailable"] == [{"product": "Test shirt (Size: M)", "requested": 6, "units_left": 5}]


def test_confirming_by_yes_takes_stock_from_the_variant(session, shop):
    prepare_draft(session, shop["customer"], [("TEST-SHIRT-M", 2)], T0)
    result = confirm_draft(session, shop["customer"], LATER, LATER)
    assert result["placed"] and result["order"]["items"][0]["product"] == "Test shirt (Size: M)"
    stock = {v["sku"]: v["stock_quantity"] for v in shop["shirt"].variants}
    assert stock == {"TEST-SHIRT-M": 3, "TEST-SHIRT-L": 0, "TEST-SHIRT-XL": 9}


def test_confirming_the_form_with_a_changed_option_and_quantity(session, shop):
    draft_id = prepare_draft(session, shop["customer"], [("TEST-SHIRT-M", 1)], T0)["form"]["draft_id"]
    order = confirm_form(session, shop["customer"], draft_id, [("TEST-TEE", 1), ("TEST-EBOOK", 3)],
                         Decimal("53.97"), LATER)
    assert order.total_amount == Decimal("53.97") and order.customer_id == shop["customer"]
    assert {i.product_name: i.quantity for i in order.items} == {"Test tee": 1, "Test e-book": 3}
    assert drafts(session, shop["customer"])[0].status == "placed"
    assert shop["shirt"].variants[0]["stock_quantity"] == 5  # the shirt wasn't ordered in the end


def test_the_form_records_the_variant_on_the_order(session, shop):
    draft_id = prepare_draft(session, shop["customer"], [("TEST-SHIRT-M", 1)], T0)["form"]["draft_id"]
    order = confirm_form(session, shop["customer"], draft_id, [("TEST-SHIRT-M", 2)], Decimal("48.00"), LATER)
    [item] = order.items
    assert item.variant_sku == "TEST-SHIRT-M" and item.product_name == "Test shirt (Size: M)"


def test_the_form_can_be_confirmed_in_the_same_request_because_a_click_is_explicit(session, shop):
    draft_id = prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)["form"]["draft_id"]
    assert confirm_form(session, shop["customer"], draft_id, [("TEST-TEE", 1)], Decimal("24.00"), T0)


def test_a_total_other_than_the_one_shown_orders_nothing(session, shop):
    draft_id = prepare_draft(session, shop["customer"], [("TEST-TEE", 2)], T0)["form"]["draft_id"]
    shop["tee"].price = Decimal("25.00")
    session.flush()
    with pytest.raises(ConflictError, match="total is now 50.00"):
        with session.begin_nested():  # the API request rolls back the same way
            confirm_form(session, shop["customer"], draft_id, [("TEST-TEE", 2)], Decimal("48.00"), LATER)
    assert shop["tee"].stock_quantity == 5
    assert session.scalar(select(Order.id).where(Order.customer_id == shop["customer"])) is None


@pytest.mark.parametrize(
    "lines, error",
    [([("TEST-SHIRT-L", 1)], ConflictError), ([("TEST-TEE", 6)], ConflictError),
     ([("TEST-SHIRT-XL", 1)], InvalidRequestError), ([("TEST-SHIRT", 1)], InvalidRequestError),
     ([("TEST-GONE", 1)], InvalidRequestError), ([("NOPE", 1)], InvalidRequestError)],
)
def test_unavailable_choices_are_refused(session, shop, lines, error):
    draft_id = prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)["form"]["draft_id"]
    with pytest.raises(error):
        with session.begin_nested():
            confirm_form(session, shop["customer"], draft_id, lines, Decimal("0.00"), LATER)


def test_forms_are_single_use_personal_and_expire(session, shop):
    draft_id = prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)["form"]["draft_id"]
    with pytest.raises(NotFoundError):
        confirm_form(session, shop["other"], draft_id, [("TEST-TEE", 1)], Decimal("24.00"), LATER)
    late = T0 + DRAFT_LIFETIME + timedelta(seconds=1)
    with pytest.raises(ConflictError, match="expired"):
        confirm_form(session, shop["customer"], draft_id, [("TEST-TEE", 1)], Decimal("24.00"), late)

    confirm_form(session, shop["customer"], draft_id, [("TEST-TEE", 1)], Decimal("24.00"), LATER)
    with pytest.raises(ConflictError, match="already used"):
        confirm_form(session, shop["customer"], draft_id, [("TEST-TEE", 1)], Decimal("24.00"), LATER)


def test_a_newer_quote_retires_the_older_form(session, shop):
    old = prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)["form"]["draft_id"]
    prepare_draft(session, shop["customer"], [("TEST-EBOOK", 1)], T0)
    with pytest.raises(ConflictError):
        confirm_form(session, shop["customer"], old, [("TEST-TEE", 1)], Decimal("24.00"), LATER)


def test_discarding_the_form(session, shop):
    draft_id = prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)["form"]["draft_id"]
    discard_form(session, shop["customer"], draft_id, LATER)
    assert drafts(session, shop["customer"])[0].status == "superseded"
    assert confirm_draft(session, shop["customer"], LATER, LATER)["placed"] is False  # "yes" can't revive it


# ---------------------------------------------------------------- the cart (products just shown)


def test_a_cart_lists_buyable_products_at_quantity_zero(session, shop):
    form = prepare_cart(
        session, shop["customer"], ["TEST-SHIRT", "TEST-TEE", "TEST-GONE", "NOPE", "test-tee"], T0
    )
    assert form["kind"] == "cart" and form["total"] == "0.00"
    shirt, tee = form["lines"]  # withdrawn, unknown and repeated products are left out
    assert (shirt["sku"], shirt["quantity"]) == ("TEST-SHIRT-M", 0)  # an in-stock size preselected
    assert [(o["sku"], o["max_quantity"]) for o in shirt["options"]] == [("TEST-SHIRT-M", 5), ("TEST-SHIRT-L", 0)]
    assert (tee["sku"], tee["quantity"]) == ("TEST-TEE", 0)
    assert shop["tee"].stock_quantity == 5
    assert session.scalar(select(Order.id).where(Order.customer_id == shop["customer"])) is None


def test_no_cart_when_nothing_can_be_bought(session, shop):
    shop["tee"].stock_quantity = 0
    assert prepare_cart(session, shop["customer"], ["TEST-TEE", "TEST-GONE"], T0) is None
    assert drafts(session, shop["customer"]) == []


def test_a_yes_in_the_chat_cannot_place_a_cart(session, shop):
    prepare_cart(session, shop["customer"], ["TEST-TEE"], T0)
    result = confirm_draft(session, shop["customer"], request_started_at=LATER, now=LATER)
    assert result["placed"] is False and "no order waiting" in result["reason"]
    assert shop["tee"].stock_quantity == 5


def test_a_cart_retires_an_earlier_quote(session, shop):
    old = prepare_draft(session, shop["customer"], [("TEST-TEE", 1)], T0)["form"]["draft_id"]
    prepare_cart(session, shop["customer"], ["TEST-EBOOK"], LATER)
    assert confirm_draft(session, shop["customer"], LATER, LATER)["placed"] is False
    with pytest.raises(ConflictError):
        confirm_form(session, shop["customer"], old, [("TEST-TEE", 1)], Decimal("24.00"), LATER)


def test_ordering_from_the_cart(session, shop):
    form = prepare_cart(session, shop["customer"], ["TEST-SHIRT", "TEST-TEE"], T0)
    order = confirm_form(session, shop["customer"], form["draft_id"], [("TEST-SHIRT-M", 2)], Decimal("48.00"), T0)
    assert order.total_amount == Decimal("48.00") and [i.variant_sku for i in order.items] == ["TEST-SHIRT-M"]
    with pytest.raises(ConflictError):  # single use, like any form
        confirm_form(session, shop["customer"], form["draft_id"], [("TEST-TEE", 1)], Decimal("24.00"), LATER)
