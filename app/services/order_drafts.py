"""
app/services/order_drafts.py

Ordering through the support bot, in two steps:

  1. prepare_draft  -- resolve SKUs, check availability and price, save an
                       OrderDraft (a quote) and return a summary for the
                       customer. Nothing is ordered or reserved.
  2. confirm_draft  -- place the customer's open draft as a real order, via
                       the same create_order() the REST API uses.

Confirming is refused unless the draft was prepared in an EARLIER chat
request (so the customer has actually seen the summary), it hasn't expired,
and the total is unchanged. The tool layer additionally requires the
customer's latest message to read as a confirmation (is_confirmation).
These checks live in code, not just in the prompt, so a confused or
prompt-injected model can't place an order on its own.

prepare_draft also returns an order FORM (for the web chat): each line with
its options -- the product's variants (sizes, colours...) and their prices --
so the customer can adjust quantity/option, see the total and confirm with a
button. That goes through confirm_form / discard_form via the
REST API: a click by the signed-in customer is itself the confirmation, and
the total the customer saw must still be the total.

prepare_cart makes the same form for products the customer was only SHOWN (a
product search): every quantity starts at 0 and the customer fills it in. Its
draft has no lines, so it can be placed from the form but never by a "yes" in
the chat -- the customer hasn't asked for anything yet.

Like orders.py, functions here don't commit; the caller does.
"""

import re
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import OrderDraft
from app.services import variants
from app.services.errors import ConflictError, DomainError, InvalidRequestError, NotFoundError
from app.services.orders import MAX_QUANTITY_PER_LINE, create_order

DRAFT_LIFETIME = timedelta(minutes=15)
MAX_LINES = 10

_NEGATIVE = r"no|nope|not|don'?t|do not|stop|wait|hold on|never ?mind|change|keep"
_AFFIRMATIVE = (
    r"yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|go ahead|proceed|do it|please do|"
    r"sounds good|correct|that'?s right"
)
_CONFIRMS = {
    "order": re.compile(
        rf"\b({_AFFIRMATIVE}|place (it|the order|my order|order)|order it|buy it)\b", re.IGNORECASE
    ),
    "cancel": re.compile(rf"\b({_AFFIRMATIVE}|cancel( it| (the|my|that) order)?)\b", re.IGNORECASE),
}
_REFUSES = {
    # Confirming an order: "cancel" means no.
    "order": re.compile(rf"\b({_NEGATIVE}|cancel)\b", re.IGNORECASE),
    "cancel": re.compile(rf"\b({_NEGATIVE})\b", re.IGNORECASE),
}


def is_confirmation(message: str, action: str = "order") -> bool:
    """True if the customer's message reads as a plain yes to `action`
    ("order" or "cancel"). Deliberately strict: anything hesitant or
    negative is not a yes, and the bot just asks again."""
    message = message or ""
    return bool(_CONFIRMS[action].search(message)) and not _REFUSES[action].search(message)


class _PriceChanged(Exception):
    pass


def _max_quantity(product, variant) -> int:
    stock = variants.stock_of(product, variant)
    return MAX_QUANTITY_PER_LINE if stock is None else min(stock, MAX_QUANTITY_PER_LINE)


def _item_sku(product, variant) -> str:
    return variant["sku"] if variant else product.sku


def _form_line(product, sku: str, quantity: int) -> dict:
    """One line of a form: the selected SKU and everything it could be instead."""
    return {
        "sku": sku,
        "quantity": quantity,
        "options": [
            {"sku": _item_sku(p, v), "name": variants.display_name(p, v),
             "unit_price": str(variants.unit_price(p, v)),
             "billing_period": p.billing_period, "max_quantity": _max_quantity(p, v)}
            for p, v in variants.sellable_items(product)
        ],
    }


def _form(draft: OrderDraft, chosen: dict) -> dict:
    """chosen: item SKU -> product, for each draft line."""
    return {
        "kind": "order",
        "draft_id": draft.id,
        "lines": [_form_line(chosen[l["sku"]], l["sku"], l["quantity"]) for l in draft.lines],
        "total": str(draft.total_amount),
        "expires_at": draft.expires_at.isoformat(),
    }


def _new_draft(
    session: Session, customer_id: int, lines: list, total: Decimal, now: datetime
) -> OrderDraft:
    # One open draft per customer: a new quote replaces any earlier one, so
    # "confirm" can never place a stale order the customer moved on from.
    session.execute(
        update(OrderDraft)
        .where(OrderDraft.customer_id == customer_id, OrderDraft.status == "open")
        .values(status="superseded")
    )
    draft = OrderDraft(
        customer_id=customer_id, lines=lines, total_amount=total, status="open",
        created_at=now, expires_at=now + DRAFT_LIFETIME,
    )
    session.add(draft)
    session.flush()
    return draft


def _order_summary(order) -> dict:
    return {
        "order_id": order.id,
        "status": order.status,
        "total": str(order.total_amount),
        "items": [
            {"product": i.product_name, "quantity": i.quantity, "unit_price": str(i.unit_price)}
            for i in order.items
        ],
    }


def prepare_draft(
    session: Session, customer_id: int, lines: list[tuple[str, int]], now: datetime
) -> dict:
    """lines: (sku, quantity). Returns the summary, or {"error": ...} with
    nothing saved."""
    wanted: dict[str, int] = {}
    for sku, quantity in lines:
        key = sku.strip().upper()
        wanted[key] = wanted.get(key, 0) + quantity

    if len(wanted) > MAX_LINES:
        return {"error": f"At most {MAX_LINES} different products per order."}
    too_many = [sku for sku, q in wanted.items() if q > MAX_QUANTITY_PER_LINE]
    if too_many:
        return {"error": f"At most {MAX_QUANTITY_PER_LINE} of any one product per order: {too_many}"}

    items: dict[tuple[int, str | None], int] = {}
    found = {}
    unknown, defaulted = [], []
    for sku, quantity in wanted.items():
        resolved = variants.resolve_sku(session, sku)
        if resolved is None:
            unknown.append(sku)
            continue
        product, variant = resolved
        if variant is None and product.variants and product.is_active:
            # The base product was named: preselect a variant (in stock if
            # possible); the customer picks the one they want in the form.
            choices = variants.sellable_items(product)
            in_stock = [(p, v) for p, v in choices if _max_quantity(p, v) > 0]
            if choices:
                product, variant = (in_stock or choices)[0]
                defaulted.append(variants.display_name(product, variant))
        if not variants.is_sellable(product, variant):
            unknown.append(sku)
            continue
        key = (product.id, variant["sku"] if variant else None)
        items[key] = items.get(key, 0) + quantity
        found[key] = (product, variant)

    if unknown:
        return {
            "error": f"No product for SKU(s) {unknown}. Use search_products to find the exact "
            "SKU, or tell the customer the product isn't available."
        }

    short = [
        {"product": variants.display_name(*found[key]), "requested": q, "units_left": stock}
        for key, q in items.items()
        if (stock := variants.stock_of(*found[key])) is not None and stock < q
    ]
    if short:
        return {"error": "Not enough stock.", "unavailable": short}

    draft_lines = []
    chosen = {}
    total = Decimal("0.00")
    for key, quantity in items.items():
        product, variant = found[key]
        price = variants.unit_price(product, variant)
        sku = _item_sku(product, variant)
        draft_lines.append(
            {"product_id": product.id, "variant_sku": key[1], "sku": sku,
             "name": variants.display_name(product, variant),
             "unit_price": str(price), "quantity": quantity}
        )
        chosen[sku] = product
        total += price * quantity

    draft = _new_draft(session, customer_id, draft_lines, total, now)

    return {
        "draft_ready": True,
        "items": [
            {"sku": l["sku"], "product": l["name"], "quantity": l["quantity"],
             "unit_price": l["unit_price"],
             "line_total": str(Decimal(l["unit_price"]) * l["quantity"])}
            for l in draft_lines
        ],
        "total": str(total),
        "expires_in_minutes": int(DRAFT_LIFETIME.total_seconds() // 60),
        **({"preselected_variants": defaulted} if defaulted else {}),
        "next_step": "Briefly tell the customer the items and total. They can adjust the "
        "quantity or option and confirm with the order form shown below your reply, or reply "
        "yes -- call confirm_order only after they say yes in their next message.",
        # Popped by the tool layer and sent to the web chat, not to the model.
        "form": _form(draft, chosen),
    }


def prepare_cart(session: Session, customer_id: int, skus: list[str], now: datetime) -> dict | None:
    """A cart form for these products (SKUs from a product search), each at
    quantity 0 with an in-stock option preselected. Products that can't be
    bought right now are left out. Returns the form, or None -- with nothing
    saved -- if none of them can."""
    lines, seen = [], set()
    for sku in skus:
        resolved = variants.resolve_sku(session, sku)
        if resolved is None:
            continue
        product = resolved[0]
        if not product.is_active or product.id in seen:
            continue
        in_stock = [(p, v) for p, v in variants.sellable_items(product) if _max_quantity(p, v) > 0]
        if not in_stock:
            continue
        seen.add(product.id)
        lines.append(_form_line(product, _item_sku(*in_stock[0]), 0))
        if len(lines) == MAX_LINES:
            break
    if not lines:
        return None

    # No lines: there is nothing for confirm_draft (a "yes" in the chat) to
    # place. Only the form's own choices, via confirm_form, can order.
    draft = _new_draft(session, customer_id, [], Decimal("0.00"), now)
    return {
        "kind": "cart",
        "draft_id": draft.id,
        "lines": lines,
        "total": "0.00",
        "expires_at": draft.expires_at.isoformat(),
    }


def confirm_draft(
    session: Session, customer_id: int, request_started_at: datetime, now: datetime
) -> dict:
    """Place the customer's open draft. Returns {"placed": True, "order": ...}
    or {"placed": False, "reason": ...}."""
    # FOR UPDATE: two simultaneous "yes" messages can't both place it -- the
    # second waits, then finds no open draft.
    draft = session.scalar(
        select(OrderDraft)
        .where(OrderDraft.customer_id == customer_id, OrderDraft.status == "open")
        .order_by(OrderDraft.created_at.desc(), OrderDraft.id.desc())
        .limit(1)
        .with_for_update()
    )
    if draft is None or not draft.lines:  # no lines: a cart, which only its form can place
        return {"placed": False, "reason": "There is no order waiting for confirmation. "
                "Use prepare_order first."}
    if draft.expires_at <= now:
        draft.status = "expired"
        return {"placed": False, "reason": "The quote expired. Prepare the order again and ask "
                "the customer to confirm the new summary."}
    if draft.created_at >= request_started_at:
        return {"placed": False, "reason": "The customer hasn't seen this summary yet. Show it "
                "and wait for them to confirm in their next message."}

    try:
        # Savepoint: if anything fails, the stock create_order took is put back.
        with session.begin_nested():
            order = create_order(
                session, customer_id,
                [(l["product_id"], l.get("variant_sku"), l["quantity"]) for l in draft.lines],
            )
            if order.total_amount != draft.total_amount:
                raise _PriceChanged
    except _PriceChanged:
        draft.status = "superseded"
        return {"placed": False, "reason": "Prices changed since the quote. Prepare the order "
                "again and ask the customer to confirm the new total."}
    except DomainError as error:  # out of stock since the quote, product withdrawn, ...
        return {"placed": False, "reason": f"{error.detail}. Tell the customer; nothing was ordered."}

    draft.status = "placed"
    draft.order_id = order.id
    return {"placed": True, "order": _order_summary(order)}


def _open_draft(session: Session, customer_id: int, draft_id: int, now: datetime) -> OrderDraft:
    draft = session.scalar(
        select(OrderDraft)
        .where(OrderDraft.id == draft_id, OrderDraft.customer_id == customer_id)
        .with_for_update()
    )
    if draft is None:  # also someone else's draft: indistinguishable
        raise NotFoundError("Order form not found")
    if draft.status != "open":
        raise ConflictError("This order form was already used or replaced by a newer one.")
    if draft.expires_at <= now:
        raise ConflictError("This order form has expired. Ask the assistant for a new one.")
    return draft


def confirm_form(
    session: Session, customer_id: int, draft_id: int, lines: list[tuple[str, int]],
    expected_total: Decimal, now: datetime,
):
    """Place an order from the web order form: the customer's chosen SKUs and
    quantities. `expected_total` is the total the form displayed; if current
    prices give anything else, nothing is ordered (ConflictError) so the
    customer never pays a total they didn't see. Returns the Order."""
    draft = _open_draft(session, customer_id, draft_id, now)

    order_lines, unknown = [], []
    for sku, quantity in lines:
        resolved = variants.resolve_sku(session, sku)
        if resolved is None:
            unknown.append(sku)
            continue
        product, variant = resolved
        order_lines.append((product.id, variant["sku"] if variant else None, quantity))
    if unknown:
        raise InvalidRequestError(f"Product(s) not available: {unknown}")

    order = create_order(session, customer_id, order_lines)
    if order.total_amount != expected_total:
        raise ConflictError(
            f"Prices have changed: the total is now {order.total_amount}, not {expected_total}. "
            "Nothing was ordered -- please review the new total."
        )
    draft.status = "placed"
    draft.order_id = order.id
    return order


def discard_form(session: Session, customer_id: int, draft_id: int, now: datetime) -> None:
    """The customer pressed Cancel on the order form."""
    _open_draft(session, customer_id, draft_id, now).status = "superseded"

