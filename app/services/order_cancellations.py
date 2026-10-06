"""
app/services/order_cancellations.py

Cancelling an order through the support bot, in two steps (cancelling is
final, so it gets the same protection as ordering -- see order_drafts.py):

  1. request_cancellation  -- check the order is the customer's own and still
                              cancellable, remember the request, and return
                              the order for the customer to confirm.
  2. confirm_cancellation  -- cancel it (stock goes back) via the same
                              transition_order() the REST API uses.

Confirming is refused unless the request was made in an EARLIER chat request
and hasn't expired; the tool layer also requires the customer's message to be
a clear yes. Orders are always looked up by the authenticated customer's id --
even for admins, the bot only touches the customer's OWN orders.

Functions here don't commit; the caller does.
"""

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import CancellationRequest, Order
from app.services.errors import DomainError
from app.services.orders import CUSTOMER_TRANSITIONS, transition_order

REQUEST_LIFETIME = timedelta(minutes=15)

# Why an order can't be cancelled online, by status.
_NOT_CANCELLABLE = {
    "processing": "It is already being prepared or shipped, so it can't be cancelled online. "
    "The customer can email support to try to stop it, or return the items after delivery.",
    "completed": "It has already been delivered. The customer can return the items under the "
    "returns policy instead.",
    "cancelled": "It is already cancelled.",
}


def _own_order(session: Session, customer_id: int, order_id: int, *, lock: bool = False):
    stmt = select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
    return session.scalar(stmt.with_for_update() if lock else stmt)


def _summary(order: Order) -> dict:
    return {
        "order_id": order.id,
        "status": order.status,
        "total": str(order.total_amount),
        "placed": f"{order.created_at:%Y-%m-%d}",
        "items": [
            {"product": i.product_name, "quantity": i.quantity, "unit_price": str(i.unit_price)}
            for i in order.items
        ],
    }


def request_cancellation(session: Session, customer_id: int, order_id: int, now: datetime) -> dict:
    order = _own_order(session, customer_id, order_id)
    if order is None:
        # Same answer for "someone else's" and "doesn't exist".
        return {"error": f"No order with ID {order_id} exists on this customer's account."}
    if "cancelled" not in CUSTOMER_TRANSITIONS.get(order.status, set()):
        return {"cancellable": False, "order": _summary(order),
                "reason": _NOT_CANCELLABLE.get(order.status, "It can't be cancelled online.")}

    # One open request per customer, so a "yes" can only ever cancel the
    # order the customer was most recently asked about.
    session.execute(
        update(CancellationRequest)
        .where(CancellationRequest.customer_id == customer_id, CancellationRequest.status == "open")
        .values(status="superseded")
    )
    session.add(
        CancellationRequest(
            customer_id=customer_id, order_id=order.id, status="open",
            created_at=now, expires_at=now + REQUEST_LIFETIME,
        )
    )
    session.flush()
    return {
        "cancellable": True,
        "order": _summary(order),
        "next_step": "Show the customer this order and ask them to confirm they want it "
        "cancelled. Call confirm_cancellation only after they say yes in their next message.",
    }


def confirm_cancellation(
    session: Session, customer_id: int, request_started_at: datetime, now: datetime
) -> dict:
    request = session.scalar(
        select(CancellationRequest)
        .where(CancellationRequest.customer_id == customer_id, CancellationRequest.status == "open")
        .order_by(CancellationRequest.created_at.desc(), CancellationRequest.id.desc())
        .limit(1)
        .with_for_update()
    )
    if request is None:
        return {"cancelled": False, "reason": "No cancellation is waiting for confirmation. "
                "Use request_cancellation first."}
    if request.expires_at <= now:
        request.status = "expired"
        return {"cancelled": False, "reason": "The request expired. Ask again with "
                "request_cancellation and have the customer confirm."}
    if request.created_at >= request_started_at:
        return {"cancelled": False, "reason": "The customer hasn't been asked yet. Show them the "
                "order and wait for them to confirm in their next message."}

    order = _own_order(session, customer_id, request.order_id, lock=True)
    try:
        # Raises if an admin moved the order on (e.g. to processing) meanwhile.
        transition_order(session, order, "cancelled", is_admin=False)
    except DomainError:
        request.status = "superseded"
        return {"cancelled": False, "order": _summary(order),
                "reason": _NOT_CANCELLABLE.get(order.status, "It can no longer be cancelled.")}

    request.status = "done"
    return {"cancelled": True, "order": _summary(order)}
