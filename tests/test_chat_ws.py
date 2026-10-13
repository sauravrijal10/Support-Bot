"""
Tests for conversation history and the WebSocket chat (/ws/chat).

Like test_tool_calling.py they use a scripted fake model and fake stores,
so they need no network, no Postgres and no Hugging Face credits.
"""

import json
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import chat_ws
from app.services.chat_service import MAX_HISTORY_TURNS, answer_question
from app.services.errors import ServiceUnavailableError

ADA = {"id": 1, "name": "Ada Lovelace", "email": "ada@example.com", "plan": "pro", "is_admin": False}
GRACE = {"id": 2, "name": "Grace Hopper", "email": "grace@example.com", "plan": "free", "is_admin": False}
TOKENS = {"ada-token": ADA, "grace-token": GRACE}


def reply(content):
    return NS(choices=[NS(message=NS(content=content, tool_calls=None))])


class FakeLLM:
    """Answers "answer N" and records the messages of every call."""

    def __init__(self):
        self.requests = []
        self.fail_with = None
        self.pieces = []  # what stream() passes to on_text before answering

    def complete(self, messages, tools=None):
        if self.fail_with:
            raise self.fail_with
        self.requests.append(json.loads(json.dumps(messages)))
        return reply(f"answer {len(self.requests)}")

    def stream(self, messages, on_text, tools=None):
        for piece in self.pieces:
            on_text(piece)
        return self.complete(messages, tools)


class FakeKB:
    def retrieve(self, query, k=3):
        return [{"source": "faq.md", "text": "FAQ text", "score": 0.5}]


def turns(messages):
    """(role, content) of the replayed history: everything between the
    system prompt and the final (current) user message."""
    return [(m["role"], m["content"]) for m in messages[1:-1]]


# ---------------------------------------------------------------- history


def test_history_is_replayed_between_system_prompt_and_question():
    llm = FakeLLM()
    history = [{"question": "Do you sell drives?", "answer": "Yes, the Backup Drive."}]
    answer_question(llm, FakeKB(), None, "How much is it?", customer=None, history=history)

    sent = llm.requests[0]
    assert sent[0]["role"] == "system"
    assert turns(sent) == [("user", "Do you sell drives?"), ("assistant", "Yes, the Backup Drive.")]
    assert sent[-1] == {"role": "user", "content": "How much is it?"}


def test_only_the_most_recent_turns_are_replayed():
    llm = FakeLLM()
    history = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(MAX_HISTORY_TURNS + 4)]
    answer_question(llm, FakeKB(), None, "next", customer=None, history=history)

    replayed = turns(llm.requests[0])
    assert len(replayed) == 2 * MAX_HISTORY_TURNS
    assert replayed[-1] == ("assistant", f"a{MAX_HISTORY_TURNS + 3}")


# ---------------------------------------------------------------- WebSocket


@pytest.fixture
def env(monkeypatch):
    app = FastAPI()
    app.include_router(chat_ws.router)
    llm = FakeLLM()
    app.state.llm, app.state.kb, app.state.db = llm, FakeKB(), object()
    monkeypatch.setattr(chat_ws, "customer_from_token", lambda db, token: TOKENS.get(token))
    with TestClient(app) as client:
        yield NS(client=client, llm=llm)


@pytest.fixture
def ws(env):
    with env.client.websocket_connect("/ws/chat") as socket:
        assert socket.receive_json() == {"type": "ready", "customer": None}
        yield socket


def ask(ws, question):
    ws.send_json({"type": "message", "question": question})
    assert ws.receive_json() == {"type": "typing"}
    return ws.receive_json()


def test_anonymous_question_is_answered(ws, env):
    frame = ask(ws, "What plans do you offer?")
    assert frame["type"] == "answer" and frame["answer"] == "answer 1"
    assert frame["sources"] == ["faq.md"] and frame["needs_authentication"] is False
    assert "not signed in" in env.llm.requests[0][0]["content"]


def test_the_answer_is_streamed_before_the_full_answer_frame(ws, env):
    # None = the text so far was not the answer after all (see LLMClient.stream).
    env.llm.pieces = ["Let me check", None, "answer ", "1"]
    ws.send_json({"type": "message", "question": "What plans do you offer?"})
    frames = [ws.receive_json() for _ in range(6)]
    assert frames[:5] == [
        {"type": "typing"},
        {"type": "delta", "text": "Let me check"},
        {"type": "typing"},
        {"type": "delta", "text": "answer "},
        {"type": "delta", "text": "1"},
    ]
    assert frames[5]["type"] == "answer" and frames[5]["answer"] == "answer 1"


def test_anonymous_account_question_is_gated(ws, env):
    frame = ask(ws, "What is the status of order 8?")
    assert frame["needs_authentication"] is True
    assert env.llm.requests == []


def test_server_keeps_the_conversation(ws, env):
    ask(ws, "Do you sell drives?")
    ask(ws, "How much is it?")
    assert turns(env.llm.requests[1]) == [("user", "Do you sell drives?"), ("assistant", "answer 1")]


def test_auth_identifies_the_customer(ws, env):
    ws.send_json({"type": "auth", "token": "ada-token"})
    frame = ws.receive_json()
    assert frame["type"] == "auth_ok" and frame["customer"]["email"] == ADA["email"]
    assert "password_hash" not in frame["customer"]

    ask(ws, "Hello")
    assert "signed in as Ada Lovelace" in env.llm.requests[0][0]["content"]


def test_bad_token_is_rejected_and_identity_is_unchanged(ws, env):
    ws.send_json({"type": "auth", "token": "forged"})
    assert ws.receive_json() == {
        "type": "error", "code": "unauthorized", "detail": "Invalid or expired token",
    }
    ask(ws, "Hello")
    assert "not signed in" in env.llm.requests[0][0]["content"]


def test_switching_customer_clears_history(ws, env):
    ws.send_json({"type": "auth", "token": "ada-token"})
    ws.receive_json()
    ask(ws, "Ada's private question")

    ws.send_json({"type": "auth", "token": "grace-token"})
    ws.receive_json()
    ask(ws, "Hello")
    assert turns(env.llm.requests[1]) == []


def test_token_expiring_mid_conversation_signs_out_without_answering(ws, env, monkeypatch):
    ws.send_json({"type": "auth", "token": "ada-token"})
    ws.receive_json()
    monkeypatch.setattr(chat_ws, "customer_from_token", lambda db, token: None)

    ws.send_json({"type": "message", "question": "What is the status of order 8?"})
    assert ws.receive_json()["code"] == "unauthorized"
    assert ws.receive_json() == {"type": "auth_ok", "customer": None}
    assert env.llm.requests == []


def test_reset_forgets_the_conversation(ws, env):
    ask(ws, "First")
    ws.send_json({"type": "reset"})
    assert ws.receive_json() == {"type": "reset_ok"}
    ask(ws, "Second")
    assert turns(env.llm.requests[1]) == []


def test_ping(ws):
    ws.send_json({"type": "ping"})
    assert ws.receive_json() == {"type": "pong"}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"type": "nope"}),
        json.dumps({"type": "message", "question": ""}),
        json.dumps({"type": "message", "question": "x" * 2001}),
        "x" * (chat_ws.MAX_FRAME_CHARS + 1),
    ],
)
def test_invalid_frames_get_an_error_and_the_socket_stays_open(ws, raw):
    ws.send_text(raw)
    assert ws.receive_json()["code"] == "invalid_frame"
    ws.send_json({"type": "ping"})
    assert ws.receive_json() == {"type": "pong"}


def test_binary_frames_are_refused(ws):
    ws.send_bytes(b"\x00\x01")
    assert ws.receive_json()["code"] == "invalid_frame"


def test_provider_outage_becomes_an_error_frame(ws, env):
    env.llm.fail_with = ServiceUnavailableError("The assistant is temporarily unavailable.")
    frame = ask(ws, "Hello")
    assert frame == {
        "type": "error", "code": "service_unavailable",
        "detail": "The assistant is temporarily unavailable.",
    }
    env.llm.fail_with = None
    assert ask(ws, "Hello again")["type"] == "answer"


def test_unexpected_failure_is_reported_generically(ws, env):
    env.llm.fail_with = RuntimeError("secret internals")
    frame = ask(ws, "Hello")
    assert frame["code"] == "internal_error" and "secret" not in frame["detail"]
