import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.deps import chat_client_key, client_ip, customer_from_token
from app.schemas.auth import CustomerProfile
from app.schemas.chat import ChatResponse, WSAuth, WSClientFrame, WSMessage, WSPing, WSReset
from app.services.chat_service import MAX_HISTORY_TURNS, answer_question, save_turn
from app.services import rate_limit
from app.services.errors import DomainError, TooManyRequestsError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

MAX_FRAME_CHARS = 8000
INVALID_TOKEN = "Invalid or expired token"

_frames = TypeAdapter(WSClientFrame)


def _profile(customer: dict | None) -> dict | None:
    return CustomerProfile(**customer).model_dump() if customer else None


class Conversation:
    """Per-connection state: who is signed in and what was said so far.
    A signed-in customer's exchanges are also saved (conversation_id), so a
    reconnecting client can resume them by sending that id with `auth`."""

    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.state = websocket.app.state
        self.ip = client_ip(websocket)
        self.token: str | None = None
        self.customer: dict | None = None
        self.history: list[dict] = []
        self.conversation_id: int | None = None  # saved conversation (signed in only)

    async def send(self, type_: str, **data) -> None:
        await self.ws.send_json({"type": type_, **data})

    async def error(self, code: str, detail: str) -> None:
        await self.send("error", code=code, detail=detail)

    def _set_identity(self, token: str | None, customer: dict | None) -> None:
        if (customer or {}).get("id") != (self.customer or {}).get("id"):
            self.history, self.conversation_id = [], None
        self.token, self.customer = token, customer

    async def on_auth(self, frame: WSAuth) -> None:
        if frame.token is None:
            self._set_identity(None, None)
            await self.send("auth_ok", customer=None, conversation_id=None)
            return

        customer = await run_in_threadpool(customer_from_token, self.state.db, frame.token)
        if customer is None:
            await self.error("unauthorized", INVALID_TOKEN)
            return
        self._set_identity(frame.token, customer)
        if frame.conversation_id is not None:
            # Only the customer's own conversation loads; anything else
            # (someone else's, deleted) starts fresh -- auth_ok says which.
            history = await run_in_threadpool(
                self.state.db.load_conversation_history,
                customer["id"], frame.conversation_id, MAX_HISTORY_TURNS,
            )
            self.history = history or []
            self.conversation_id = frame.conversation_id if history is not None else None
        await self.send(
            "auth_ok", customer=_profile(customer), conversation_id=self.conversation_id
        )

    async def on_message(self, frame: WSMessage) -> None:
        if self.token is not None:
            customer = await run_in_threadpool(customer_from_token, self.state.db, self.token)
            if customer is None:
                # Never silently answer as anonymous: say so and sign out.
                self._set_identity(None, None)
                await self.error("unauthorized", INVALID_TOKEN)
                await self.send("auth_ok", customer=None, conversation_id=None)
                return
            self.customer = customer

        try:
            rate_limit.take_chat(chat_client_key(self.customer, self.ip))
        except TooManyRequestsError as error:
            await self.error("rate_limited", error.detail)
            return

        await self.send("typing")
        loop = asyncio.get_running_loop()

        def on_text(text: str | None) -> None:
            # Called from the worker thread. Waiting for the send keeps the
            # frames in order and stops the model call if the client has gone.
            frame = self.send("typing") if text is None else self.send("delta", text=text)
            asyncio.run_coroutine_threadsafe(frame, loop).result()

        try:
            result = await run_in_threadpool(
                answer_question,
                llm=self.state.llm,
                kb=self.state.kb,
                db=self.state.db,
                query=frame.question,
                customer=self.customer,
                history=self.history,
                on_text=on_text,
            )
        except DomainError as error:
            await self.error("service_unavailable", error.detail)
            return

        self.history.append({"question": frame.question, "answer": result.answer})
        del self.history[:-MAX_HISTORY_TURNS]
        self.conversation_id = await run_in_threadpool(
            save_turn, self.state.db, self.customer, self.conversation_id, frame.question, result
        )

        response = ChatResponse(
            answer=result.answer,
            sources=result.sources,
            needs_authentication=result.needs_authentication,
            tools_used=result.tools_used,
            order_form=result.order_form,
            conversation_id=self.conversation_id,
        )
        await self.send("answer", **response.model_dump())

    async def handle(self, text: str) -> None:
        if len(text) > MAX_FRAME_CHARS:
            await self.error("invalid_frame", "Message too large")
            return
        try:
            frame = _frames.validate_json(text)
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            await self.error("invalid_frame", f"{where}: {first['msg']}" if where else first["msg"])
            return

        if isinstance(frame, WSAuth):
            await self.on_auth(frame)
        elif isinstance(frame, WSMessage):
            await self.on_message(frame)
        elif isinstance(frame, WSReset):
            # The saved conversation stays; the next message starts a new one.
            self.history, self.conversation_id = [], None
            await self.send("reset_ok")
        elif isinstance(frame, WSPing):
            await self.send("pong")


@router.websocket("/ws/chat")
async def chat_socket(websocket: WebSocket) -> None:
    ip = client_ip(websocket)
    if not rate_limit.WEBSOCKETS_PER_IP.acquire(ip):
        # 1013 "try again later": too many open chats from this address.
        await websocket.close(code=1013)
        return
    try:
        await _converse(websocket)
    finally:
        rate_limit.WEBSOCKETS_PER_IP.release(ip)


async def _converse(websocket: WebSocket) -> None:
    await websocket.accept()
    conversation = Conversation(websocket)
    await conversation.send("ready", customer=None)

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                await conversation.error("invalid_frame", "Send JSON as text frames")
                continue
            try:
                await conversation.handle(text)
            except WebSocketDisconnect:
                raise
            except Exception:
                # Keep the conversation alive; details stay in the server log.
                logger.exception("WebSocket chat frame failed")
                await conversation.error("internal_error", "Something went wrong. Please try again.")
    except WebSocketDisconnect:
        pass
