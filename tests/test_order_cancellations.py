"""
Tests for cancelling orders through the bot (app/services/order_cancellations.py).

Like test_order_drafts.py these run against Postgres (DATABASE_URL) inside a
rolled-back transaction, and are skipped without a database.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import CancellationRequest, Customer, Product, ProductType
from app.services.order_cancellations import (
    REQUEST_LIFETIME,
    confirm_cancellation,
    request_cancellation,
)
from app.services.order_drafts import is_confirmation
from app.services.orders import create_order

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
LATER = T0 + timedelta(minutes=1)


# ---------------------------------------------------------------- wording (no DB)


@pytest.mark.parametrize("message", ["yes", "Yes, cancel it", "cancel it please", "ok go ahead", "cancel"])
def test_clear_yes_to_cancelling(message):
    assert is_confirmation(message, "cancel")


@pytest.mark.parametrize("message", ["no", "don't cancel", "no, keep it", "wait", "what's my order status?"])
def test_not_a_yes_to_cancelling(message):
    assert not is_confirmation(message, "cancel")


def test_cancel_means_no_when_confirming_an_order():
    assert is_confirmation("yes cancel it", "cancel") and not is_confirmation("yes cancel it", "order")


# ---------------------------------------------------------------- with Postgres


@pytest.fixture
def shop(session):
    kind = ProductType(slug="test-kind-cancel", name="Test kind", attribute_schema=[])
    customer = Customer(name="Cancel Tester", email="cancel-tester@example.test")
    other = Customer(name="Other", email="cancel-other@example.test", is_admin=True)
    tee = Product(sku="TEST-CANCEL-TEE", name="Test tee", price=Decimal("24.00"), stock_quantity=5, product_type=kind)
    session.add_all([kind, customer, other, tee])
    session.flush()
    order = create_order(session, customer.id, [(tee.id, None, 2)])
    session.flush()
    return {"customer": customer.id, "other": other.id, "tee": tee, "order": order}


def requests(session, customer_id):
    return session.scalars(
        select(CancellationRequest)
        .where(CancellationRequest.customer_id == customer_id)
        .order_by(CancellationRequest.id)
    ).all()


def test_request_shows_the_order_and_changes_nothing(session, shop):
    result = request_cancellation(session, shop["customer"], shop["order"].id, T0)
    assert result["cancellable"] and result["order"]["total"] == "48.00"
    assert shop["order"].status == "pending" and shop["tee"].stock_quantity == 3
    [req] = requests(session, shop["customer"])
    assert req.status == "open" and req.expires_at == T0 + REQUEST_LIFETIME


def test_confirm_in_a_later_message_cancels_and_restocks(session, shop):
    request_cancellation(session, shop["customer"], shop["order"].id, T0)
    result = confirm_cancellation(session, shop["customer"], LATER, LATER)
    assert result["cancelled"] and result["order"]["status"] == "cancelled"
    assert shop["order"].status == "cancelled" and shop["tee"].stock_quantity == 5
    assert requests(session, shop["customer"])[0].status == "done"


def test_confirm_in_the_same_request_is_refused(session, shop):
    request_cancellation(session, shop["customer"], shop["order"].id, LATER)
    result = confirm_cancellation(session, shop["customer"], request_started_at=T0, now=LATER)
    assert result["cancelled"] is False and shop["order"].status == "pending"


def test_cancelling_twice_does_nothing_the_second_time(session, shop):
    request_cancellation(session, shop["customer"], shop["order"].id, T0)
    assert confirm_cancellation(session, shop["customer"], LATER, LATER)["cancelled"]
    again = confirm_cancellation(session, shop["customer"], LATER, LATER)
    assert again["cancelled"] is False and shop["tee"].stock_quantity == 5


def test_expired_requests_are_not_acted_on(session, shop):
    request_cancellation(session, shop["customer"], shop["order"].id, T0)
    late = T0 + REQUEST_LIFETIME + timedelta(seconds=1)
    result = confirm_cancellation(session, shop["customer"], late, late)
    assert result["cancelled"] is False and "expired" in result["reason"]
    assert shop["order"].status == "pending"


@pytest.mark.parametrize("status, hint", [("processing", "being prepared"), ("completed", "delivered"),
                                          ("cancelled", "already cancelled")])
def test_only_pending_orders_can_be_cancelled(session, shop, status, hint):
    shop["order"].status = status
    session.flush()
    result = request_cancellation(session, shop["customer"], shop["order"].id, T0)
    assert result["cancellable"] is False and hint in result["reason"]
    assert requests(session, shop["customer"]) == []


def test_an_order_that_moved_on_after_the_request_is_not_cancelled(session, shop):
    request_cancellation(session, shop["customer"], shop["order"].id, T0)
    shop["order"].status = "processing"  # an admin started on it meanwhile
    session.flush()
    result = confirm_cancellation(session, shop["customer"], LATER, LATER)
    assert result["cancelled"] is False and shop["order"].status == "processing"
    assert shop["tee"].stock_quantity == 3


def test_other_customers_orders_look_nonexistent_even_to_admins(session, shop):
    result = request_cancellation(session, shop["other"], shop["order"].id, T0)
    assert "No order with ID" in result["error"]
    assert confirm_cancellation(session, shop["other"], LATER, LATER)["cancelled"] is False
    assert shop["order"].status == "pending"


def test_a_new_request_replaces_the_old_one(session, shop):
    second = create_order(session, shop["customer"], [(shop["tee"].id, None, 1)])
    session.flush()
    request_cancellation(session, shop["customer"], shop["order"].id, T0)
    request_cancellation(session, shop["customer"], second.id, T0)
    confirm_cancellation(session, shop["customer"], LATER, LATER)
    assert shop["order"].status == "pending" and second.status == "cancelled"
