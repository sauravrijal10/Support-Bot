"""
app/services/orders.py

Order business rules: creating an order (price snapshot + stock), the
allowed status transitions, and who may see an order.

Functions here do not commit -- the calling view commits once the whole
operation succeeds, so a failure anywhere (e.g. the last line item is out
of stock) rolls back everything, including stock already decremented.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, OrderItem, Product
from app.services.errors import ConflictError, InvalidRequestError, NotFoundError

MAX_QUANTITY_PER_LINE = 100

# Admins drive the full lifecycle; completed and cancelled are final.
ADMIN_TRANSITIONS = {
    "pending": {"processing", "cancelled"},
    "processing": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
# A customer may only cancel an order that hasn't started processing.
CUSTOMER_TRANSITIONS = {"pending": {"cancelled"}}


def _lock_products(session: Session, product_ids: list[int]) -> dict[int, Product]:
    """Fetch products with row locks. Locking in id order means two orders
    touching the same products can't deadlock each other, and stock checks
    below can't race."""
    rows = session.scalars(
        select(Product)
        .where(Product.id.in_(product_ids))
        .order_by(Product.id)
        # of=Product: lock only the product rows, not their (shared) type row.
        .with_for_update(of=Product)
    ).all()
    return {p.id: p for p in rows}


def create_order(session: Session, customer_id: int, lines: list[tuple[int, int]]) -> Order:
    """lines: (product_id, quantity) pairs. Prices always come from the
    database -- the client never supplies them."""
    wanted: dict[int, int] = {}
    for product_id, quantity in lines:
        wanted[product_id] = wanted.get(product_id, 0) + quantity

    if any(q > MAX_QUANTITY_PER_LINE for q in wanted.values()):
        raise InvalidRequestError(
            f"At most {MAX_QUANTITY_PER_LINE} of any one product per order"
        )

    product_ids = sorted(wanted)
    products = _lock_products(session, product_ids)

    # Same message for "doesn't exist" and "inactive": don't reveal hidden products.
    unavailable = [pid for pid in product_ids if pid not in products or not products[pid].is_active]
    if unavailable:
        raise InvalidRequestError(f"Product(s) not available: {unavailable}")

    items = []
    total = Decimal("0.00")
    for pid in product_ids:
        product, quantity = products[pid], wanted[pid]
        if product.stock_quantity is not None:
            if product.stock_quantity < quantity:
                raise ConflictError(f"Not enough stock for '{product.name}'")
            product.stock_quantity -= quantity
        items.append(
            OrderItem(
                product_id=pid,
                product_name=product.name,
                unit_price=product.price,
                quantity=quantity,
            )
        )
        total += product.price * quantity

    order = Order(customer_id=customer_id, status="pending", total_amount=total, items=items)
    session.add(order)
    session.flush()
    return order


def _restore_stock(session: Session, order: Order) -> None:
    products = _lock_products(session, sorted({i.product_id for i in order.items}))
    for item in order.items:
        product = products.get(item.product_id)
        if product is not None and product.stock_quantity is not None:
            product.stock_quantity += item.quantity


def transition_order(session: Session, order: Order, new_status: str, *, is_admin: bool) -> None:
    allowed = ADMIN_TRANSITIONS if is_admin else CUSTOMER_TRANSITIONS
    if new_status not in allowed.get(order.status, set()):
        raise ConflictError(f"An order that is '{order.status}' cannot be changed to '{new_status}'")
    if new_status == "cancelled":
        _restore_stock(session, order)
    order.status = new_status


def get_order_for_actor(
    session: Session, order_id: int, customer: dict, *, for_update: bool = False
) -> Order:
    """The order if the caller owns it or is an admin. Otherwise NotFound --
    identical to a nonexistent order, so other customers' order ids can't be
    probed."""
    stmt = select(Order).where(Order.id == order_id)
    if for_update:
        stmt = stmt.with_for_update()
    order = session.scalar(stmt)
    if order is None or (order.customer_id != customer["id"] and not customer["is_admin"]):
        raise NotFoundError("Order not found")
    return order
