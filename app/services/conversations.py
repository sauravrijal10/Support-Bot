"""
app/services/conversations.py

Saved chat conversations, so a signed-in customer picks up where they left
off -- after a page refresh, a dropped WebSocket, or on another device.

Only signed-in customers' conversations are saved: an anonymous one has no
owner to hand it back to. Every function takes the AUTHENTICATED customer's
id and filters on it, so someone else's conversation looks exactly like one
that doesn't exist, and a turn is never added to it.

Every exchange is kept; only the last few are replayed to the model
(chat_service.MAX_HISTORY_TURNS).

Like orders.py, functions here don't commit; the caller does.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation, ConversationMessage
from app.services.errors import NotFoundError

TITLE_CHARS = 80


def _title(question: str) -> str:
    text = " ".join(question.split())
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"


def find_conversation(session: Session, customer_id: int, conversation_id: int) -> Conversation | None:
    return session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.customer_id == customer_id
        )
    )


def get_conversation(session: Session, customer_id: int, conversation_id: int) -> Conversation:
    """The customer's conversation, or NotFoundError -- also for someone else's."""
    conversation = find_conversation(session, customer_id, conversation_id)
    if conversation is None:
        raise NotFoundError("Conversation not found")
    return conversation


def history(
    session: Session, customer_id: int, conversation_id: int, turns: int
) -> list[dict] | None:
    """The last `turns` exchanges, oldest first, as {"question", "answer"} --
    the shape answer_question takes. None if it isn't this customer's."""
    if find_conversation(session, customer_id, conversation_id) is None:
        return None
    rows = session.execute(
        select(ConversationMessage.question, ConversationMessage.answer)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.id.desc())
        .limit(turns)
    ).all()
    return [{"question": q, "answer": a} for q, a in reversed(rows)]


def add_turn(
    session: Session,
    customer_id: int,
    conversation_id: int | None,
    question: str,
    answer: str,
    sources: list[str],
    tools_used: list[str],
    now: datetime,
) -> Conversation:
    """Save one exchange. It starts a new conversation when conversation_id
    is None, or isn't this customer's (e.g. deleted while the chat was open).
    Returns the conversation it was saved to."""
    conversation = None
    if conversation_id is not None:
        conversation = find_conversation(session, customer_id, conversation_id)
    if conversation is None:
        conversation = Conversation(
            customer_id=customer_id, title=_title(question), created_at=now, updated_at=now
        )
        session.add(conversation)
        session.flush()

    session.add(ConversationMessage(
        conversation_id=conversation.id, question=question, answer=answer,
        sources=list(sources), tools_used=list(tools_used), created_at=now,
    ))
    conversation.updated_at = now
    session.flush()
    return conversation
