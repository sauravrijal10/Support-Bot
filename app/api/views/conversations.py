from fastapi import Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_customer, get_session
from app.api.views.base import APIView, route
from app.models import Conversation
from app.schemas.conversation import ConversationDetail, ConversationOut
from app.services import conversations


class ConversationView(APIView):
    """The signed-in customer's saved chat conversations. Private: admins
    see only their own too."""

    prefix = "/conversations"
    tags = ["conversations"]

    @route("GET", "", response_model=list[ConversationOut])
    def list_conversations(
        self,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """Your conversations, most recently active first."""
        stmt = (
            select(Conversation)
            .where(Conversation.customer_id == customer["id"])
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return session.scalars(stmt).all()

    @route("GET", "/{conversation_id}", response_model=ConversationDetail)
    def get_conversation(
        self,
        conversation_id: int,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """One of your conversations with every exchange, oldest first."""
        conversation = conversations.get_conversation(session, customer["id"], conversation_id)
        # Built here, while the session is open, so the messages can load.
        return ConversationDetail.model_validate(conversation)

    @route("DELETE", "/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_conversation(
        self,
        conversation_id: int,
        session: Session = Depends(get_session),
        customer: dict = Depends(get_current_customer),
    ):
        """Delete one of your conversations and everything said in it."""
        session.delete(conversations.get_conversation(session, customer["id"], conversation_id))
        session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
