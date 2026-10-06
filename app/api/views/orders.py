from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_customer, get_session, require_admin
from app.api.views.base import APIView, route
from app.models import Order
from app.models.enums import OrderStatus
from app.schemas.order import OrderCreate, OrderFormConfirm, OrderOut, OrderStatusUpdate
from app.services import order_drafts
from app.services import orders as order_service


class OrderView(APIView):
    """Orders. Customers create and see only their own; admins see all and
    move orders through their lifecycle."""

    prefix = "/orders"
    tags = ["orders"]

    @route("POST", "", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
    def create_order(
        self,
        payload: OrderCreate,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """Place an order for yourself. Send only product ids and quantities --
        prices, totals and status are set by the server."""
        order = order_service.create_order(
            session, customer["id"],
            [(line.product_id, line.variant_sku, line.quantity) for line in payload.items],
        )
        session.commit()
        return order

    @route("GET", "", response_model=list[OrderOut])
    def list_orders(
        self,
        status_filter: OrderStatus | None = Query(None, alias="status"),
        customer_id: int | None = Query(None, description="Admins only"),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """Your own orders, newest first. Admins see everyone's."""
        stmt = select(Order)
        if customer["is_admin"]:
            if customer_id is not None:
                stmt = stmt.where(Order.customer_id == customer_id)
        else:
            if customer_id is not None and customer_id != customer["id"]:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can filter by customer")
            stmt = stmt.where(Order.customer_id == customer["id"])

        if status_filter is not None:
            stmt = stmt.where(Order.status == status_filter)
        stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).offset(offset)
        return session.scalars(stmt).all()

    @route("GET", "/{order_id}", response_model=OrderOut)
    def get_order(
        self,
        order_id: int,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        return order_service.get_order_for_actor(session, order_id, customer)

    @route("POST", "/{order_id}/cancel", response_model=OrderOut)
    def cancel_order(
        self,
        order_id: int,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """Cancel an order and return its items to stock. Customers can cancel
        their own order only while it is still 'pending'."""
        order = order_service.get_order_for_actor(session, order_id, customer, for_update=True)
        order_service.transition_order(session, order, "cancelled", is_admin=customer["is_admin"])
        session.commit()
        return order

    @route("PATCH", "/{order_id}/status", response_model=OrderOut)
    def update_order_status(
        self,
        order_id: int,
        payload: OrderStatusUpdate,
        session: Session = Depends(get_session),
        admin: dict = Depends(require_admin),
    ):
        order = order_service.get_order_for_actor(session, order_id, admin, for_update=True)
        order_service.transition_order(session, order, payload.status, is_admin=True)
        session.commit()
        return order

    @route(
        "POST", "/drafts/{draft_id}/confirm",
        response_model=OrderOut, status_code=status.HTTP_201_CREATED,
    )
    def confirm_order_form(
        self,
        draft_id: int,
        payload: OrderFormConfirm,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """Place the order from a chat order form (`order_form` in a /chat
        answer), with the options and quantities the customer chose. Send the
        total the form displayed as `expected_total`: if prices changed since,
        nothing is ordered (409)."""
        order = order_drafts.confirm_form(
            session, customer["id"], draft_id,
            [(line.sku, line.quantity) for line in payload.items],
            payload.expected_total, datetime.now(timezone.utc),
        )
        session.commit()
        return order

    @route("POST", "/drafts/{draft_id}/discard", status_code=status.HTTP_204_NO_CONTENT)
    def discard_order_form(
        self,
        draft_id: int,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """The customer cancelled a chat order form; nothing was ordered."""
        order_drafts.discard_form(session, customer["id"], draft_id, datetime.now(timezone.utc))
        session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

