"""
app/main.py

FastAPI entrypoint. At startup it applies database migrations, indexes the
FAQ docs into pgvector, and builds the shared clients once (stored on
app.state) so each request reuses them.

Run with:
  uvicorn app.main:app --reload

Then open / in a browser for the chat UI (REST or WebSocket).
"""

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import auth, chat, chat_ws, health, web
from app.api.views.conversations import ConversationView
from app.api.views.orders import OrderView
from app.api.views.product_types import ProductTypeView
from app.api.views.products import ProductView
from app.api.views.tickets import TicketView
from app.config import get_settings
from app.db.migrate import run_migrations
from app.db.session import create_session_factory
from app.services.database import Database
from app.services.errors import DomainError
from app.services.llm import LLMClient
from app.services.vector_knowledge_base import VectorKnowledgeBase

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    run_migrations()
    engine, session_factory = create_session_factory(settings.database_url)

    kb = VectorKnowledgeBase(settings.database_url)
    kb.load_folder(settings.knowledge_dir)

    app.state.session_factory = session_factory
    app.state.db = Database(session_factory)
    app.state.kb = kb
    app.state.llm = LLMClient(
        list(settings.hf_tokens), settings.llm_model, settings.llm_provider, settings.llm_fallbacks
    )

    yield

    engine.dispose()


app = FastAPI(title="Support Bot API", lifespan=lifespan)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, error: DomainError):
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail},
        headers=getattr(error, "headers", None),
    )


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(chat_ws.router)
app.include_router(web.router)
app.include_router(ProductTypeView.as_router())
app.include_router(ProductView.as_router())
app.include_router(OrderView.as_router())
app.include_router(TicketView.as_router())
app.include_router(ConversationView.as_router())
