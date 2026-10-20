"""
Tests for saved conversations (app/services/conversations.py) and their
REST endpoints (app/api/views/conversations.py).

These need Postgres, so they run against DATABASE_URL and are SKIPPED when it
isn't set or reachable. Each test runs inside a transaction that is rolled
back afterwards. With docker compose:

    docker compose run --rm app sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import deps
from app.api.views.conversations import ConversationView
from app.main import domain_error_handler
from app.models import Conversation, ConversationMessage, Customer
from app.services.conversations import TITLE_CHARS, add_turn, history
from app.services.errors import DomainError

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def people(session):
    ada = Customer(name="Convo Ada", email="convo-ada@example.test")
    grace = Customer(name="Convo Grace", email="convo-grace@example.test")
    session.add_all([ada, grace])
    session.flush()
    return {"ada": ada.id, "grace": grace.id}


def turn(session, customer_id, conversation_id, question, when=T0):
    return add_turn(
        session, customer_id, conversation_id, question, f"re: {question}", ["faq.md"], ["search_products"], when
    )


# ---------------------------------------------------------------- the service


def test_the_first_turn_starts_a_conversation_and_later_ones_join_it(session, people):
    first = turn(session, people["ada"], None, "Do you sell drives?")
    later = T0 + timedelta(minutes=2)
    second = turn(session, people["ada"], first.id, "How much is it?", later)
    assert first.id == second.id and first.title == "Do you sell drives?"
    assert first.created_at == T0 and first.updated_at == later
    messages = session.scalars(
        select(ConversationMessage).where(ConversationMessage.conversation_id == first.id)
    ).all()
    assert [(m.question, m.sources, m.tools_used) for m in messages] == [
        ("Do you sell drives?", ["faq.md"], ["search_products"]),
        ("How much is it?", ["faq.md"], ["search_products"]),
    ]


def test_long_titles_are_shortened(session, people):
    conversation = turn(session, people["ada"], None, "word  " * 40)
    assert len(conversation.title) == TITLE_CHARS and conversation.title.endswith("…")


def test_a_turn_is_never_added_to_someone_elses_conversation(session, people):
    graces = turn(session, people["grace"], None, "Grace's question")
    adas = turn(session, people["ada"], graces.id, "Ada's question")
    assert adas.id != graces.id and adas.customer_id == people["ada"]
    assert history(session, people["grace"], graces.id, 10) == [
        {"question": "Grace's question", "answer": "re: Grace's question"}
    ]


def test_history_is_the_latest_turns_oldest_first_and_only_for_the_owner(session, people):
    conversation = turn(session, people["ada"], None, "q0")
    for i in range(1, 5):
        turn(session, people["ada"], conversation.id, f"q{i}")
    assert [t["question"] for t in history(session, people["ada"], conversation.id, 3)] == ["q2", "q3", "q4"]
    assert history(session, people["grace"], conversation.id, 3) is None
    assert history(session, people["ada"], 999999, 3) is None


# ---------------------------------------------------------------- the REST endpoints


@pytest.fixture
def api(session, people):
    app = FastAPI()
    app.include_router(ConversationView.as_router())
    app.add_exception_handler(DomainError, domain_error_handler)
    who = {"id": people["ada"], "is_admin": False}
    app.dependency_overrides[deps.get_session] = lambda: session
    app.dependency_overrides[deps.get_current_customer] = lambda: who
    with TestClient(app) as client:
        yield client, who


def test_listing_shows_only_your_conversations_most_recent_first(session, people, api):
    client, _ = api
    old = turn(session, people["ada"], None, "Old question")
    new = turn(session, people["ada"], None, "New question", T0 + timedelta(hours=1))
    turn(session, people["grace"], None, "Grace's question", T0 + timedelta(hours=2))
    listed = client.get("/conversations").json()
    assert [c["id"] for c in listed] == [new.id, old.id]
    assert client.get("/conversations?limit=1").json()[0]["title"] == "New question"


def test_reading_a_conversation_returns_every_exchange(session, people, api):
    client, _ = api
    conversation = turn(session, people["ada"], None, "First")
    turn(session, people["ada"], conversation.id, "Second")
    body = client.get(f"/conversations/{conversation.id}").json()
    assert [m["question"] for m in body["messages"]] == ["First", "Second"]
    assert body["messages"][0]["answer"] == "re: First"


def test_someone_elses_conversation_is_not_found_even_for_an_admin(session, people, api):
    client, who = api
    graces = turn(session, people["grace"], None, "Grace's question")
    who["is_admin"] = True
    assert client.get(f"/conversations/{graces.id}").status_code == 404
    assert client.delete(f"/conversations/{graces.id}").status_code == 404
    assert client.get("/conversations/999999").status_code == 404


def test_deleting_a_conversation_removes_its_messages(session, people, api):
    client, _ = api
    conversation = turn(session, people["ada"], None, "Forget this")
    assert client.delete(f"/conversations/{conversation.id}").status_code == 204
    assert session.get(Conversation, conversation.id) is None
    remaining = session.scalar(
        select(func.count()).where(ConversationMessage.conversation_id == conversation.id)
    )
    assert remaining == 0
