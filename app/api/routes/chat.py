from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import chat_client_key, client_ip, get_optional_customer
from app.services import rate_limit
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import MAX_HISTORY_TURNS, answer_question, save_turn
from app.services.errors import NotFoundError

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def ask(
    payload: ChatRequest,
    request: Request,
    customer: dict | None = Depends(get_optional_customer),
) -> ChatResponse:
    """Answer a question. Send `Authorization: Bearer <token>` to get
    answers about your own account, orders and tickets; without it only
    general FAQ questions are answered.

    Signed in, every exchange is saved: the response's `conversation_id`,
    sent back with the next question, continues that conversation with its
    stored history. Signed out, nothing is saved -- pass earlier turns in
    `history` for follow-up questions (or use the WebSocket at /ws/chat)."""
    # Before anything costs money: each question can mean several LLM calls.
    rate_limit.take_chat(chat_client_key(customer, client_ip(request)))
    db = request.app.state.db
    history = [turn.model_dump() for turn in payload.history]
    conversation_id = payload.conversation_id
    if conversation_id is not None:
        if customer is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Sign in to continue a saved conversation",
                headers={"WWW-Authenticate": "Bearer"},
            )
        history = db.load_conversation_history(customer["id"], conversation_id, MAX_HISTORY_TURNS)
        if history is None:
            raise NotFoundError("Conversation not found")

    result = answer_question(
        llm=request.app.state.llm,
        kb=request.app.state.kb,
        db=db,
        query=payload.question,
        customer=customer,
        history=history,
    )
    return ChatResponse(
        answer=result.answer,
        sources=result.sources,
        needs_authentication=result.needs_authentication,
        tools_used=result.tools_used,
        order_form=result.order_form,
        conversation_id=save_turn(db, customer, conversation_id, payload.question, result),
    )
