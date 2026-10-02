from fastapi import APIRouter, Depends, Request

from app.api.deps import get_optional_customer
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import answer_question

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def ask(
    payload: ChatRequest,
    request: Request,
    customer: dict | None = Depends(get_optional_customer),
) -> ChatResponse:
    """Answer a question. Send `Authorization: Bearer <token>` to get
    answers about your own account, orders and tickets; without it only
    general FAQ questions are answered. Pass earlier turns in `history`
    for follow-up questions (this endpoint keeps no state; for a stateful
    conversation use the WebSocket at /ws/chat)."""
    result = answer_question(
        llm=request.app.state.llm,
        kb=request.app.state.kb,
        db=request.app.state.db,
        query=payload.question,
        customer=customer,
        history=[turn.model_dump() for turn in payload.history],
    )
    return ChatResponse(
        answer=result.answer,
        sources=result.sources,
        needs_authentication=result.needs_authentication,
        tools_used=result.tools_used,
    )
