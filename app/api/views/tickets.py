from fastapi import Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_customer, get_session, require_admin
from app.api.views.base import APIView, route
from app.models import Ticket
from app.models.enums import TicketStatus
from app.schemas.ticket import TicketCreate, TicketOut, TicketStatusUpdate


def _get_ticket_for_actor(session: Session, ticket_id: int, customer: dict) -> Ticket:
    """The ticket if the caller owns it or is an admin; otherwise a 404
    identical to a nonexistent ticket."""
    ticket = session.get(Ticket, ticket_id)
    if ticket is None or (ticket.customer_id != customer["id"] and not customer["is_admin"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return ticket


class TicketView(APIView):
    """Support tickets. Customers open and see their own; admins see all and
    change status."""

    prefix = "/tickets"
    tags = ["tickets"]

    @route("POST", "", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
    def create_ticket(
        self,
        payload: TicketCreate,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        ticket = Ticket(
            customer_id=customer["id"],
            subject=payload.subject,
            description=payload.description,
            status="open",
        )
        session.add(ticket)
        session.commit()
        return ticket

    @route("GET", "", response_model=list[TicketOut])
    def list_tickets(
        self,
        status_filter: TicketStatus | None = Query(None, alias="status"),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """Your own tickets, newest first. Admins see everyone's."""
        stmt = select(Ticket)
        if not customer["is_admin"]:
            stmt = stmt.where(Ticket.customer_id == customer["id"])
        if status_filter is not None:
            stmt = stmt.where(Ticket.status == status_filter)
        stmt = stmt.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit).offset(offset)
        return session.scalars(stmt).all()

    @route("GET", "/{ticket_id}", response_model=TicketOut)
    def get_ticket(
        self,
        ticket_id: int,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        return _get_ticket_for_actor(session, ticket_id, customer)

    @route("POST", "/{ticket_id}/close", response_model=TicketOut)
    def close_ticket(
        self,
        ticket_id: int,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """Close your own ticket (e.g. the problem went away)."""
        ticket = _get_ticket_for_actor(session, ticket_id, customer)
        ticket.status = "closed"
        session.commit()
        return ticket

    @route("PATCH", "/{ticket_id}/status", response_model=TicketOut)
    def update_ticket_status(
        self,
        ticket_id: int,
        payload: TicketStatusUpdate,
        session: Session = Depends(get_session),
        admin: dict = Depends(require_admin),
    ):
        ticket = _get_ticket_for_actor(session, ticket_id, admin)
        ticket.status = payload.status
        session.commit()
        return ticket
