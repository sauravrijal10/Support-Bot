"""
Tests for the tool-calling loop and the tools themselves.

They use a scripted fake LLM and a fake database, so they need no network,
no Postgres and no Hugging Face credits. Run:

    docker compose run --rm app sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
"""

import json
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import httpx
import pytest
from huggingface_hub.errors import HfHubHTTPError

from app.services import chat_service
from app.services.chat_service import (
    LOGIN_REQUIRED_MESSAGE,
    MAX_CALLS_PER_STEP,
    MAX_TOOL_STEPS,
    answer_question,
)
from app.services.errors import ServiceUnavailableError
from app.services.llm import LLMClient
from app.services.tools import ALL_TOOLS, ToolContext, run_tool, search_terms, tools_for

ADA = {"id": 1, "name": "Ada Lovelace", "email": "ada@example.com", "plan": "pro", "is_admin": False}
WHEN = datetime(2026, 9, 15)


# ---------------------------------------------------------------- fakes


def tool_call(name, arguments, call_id="call_1"):
    return NS(id=call_id, function=NS(name=name, arguments=arguments))


def reply(content=None, calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))])


class FakeLLM:
    """Plays back a scripted list of replies and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []  # (messages snapshot, tool names offered)

    def complete(self, messages, tools=None):
        offered = [t["function"]["name"] for t in tools] if tools else None
        self.requests.append((json.loads(json.dumps(messages)), offered))
        return self.replies.pop(0)


class FakeKB:
    def retrieve(self, query, k=3):
        return [{"source": "faq.md", "text": "## Refunds\nFull refund within 14 days.", "score": 0.5}]


class FakeDB:
    """Order 8 belongs to customer 1 (Ada); order 2 belongs to customer 2."""

    def __init__(self):
        self.order_lookups = []
        self.product_searches = []
        self.fail = False

    def get_order_for_customer(self, order_id, customer_id):
        self.order_lookups.append((order_id, customer_id))
        if self.fail:
            raise RuntimeError("db exploded")
        owner = {8: 1, 2: 2}.get(order_id)
        if owner != customer_id:
            return None
        return {
            "id": order_id, "status": "processing", "total_amount": Decimal("328.00"),
            "created_at": WHEN,
            "items": [{"product_name": "Pro plan - annual", "unit_price": Decimal("150.00"), "quantity": 1}],
        }

    def get_orders_for_customer(self, customer_id, status=None, limit=20):
        return []

    def get_tickets_for_customer(self, customer_id, status=None, limit=20):
        return []

    def search_products(self, terms, max_price=None, in_stock_only=False, limit=10):
        self.product_searches.append({"terms": terms, "max_price": max_price, "in_stock_only": in_stock_only})
        return [{"sku": "PLUS-PLAN", "name": "Plus plan", "description": "100GB", "price": Decimal("5.00"),
                 "billing_period": "monthly", "stock_quantity": None}], 1


def ask(llm, query="What is the status of order 8?", customer=ADA, db=None):
    db = db or FakeDB()
    return answer_question(llm, FakeKB(), db, query, customer), db


# ---------------------------------------------------------------- the loop


def test_signed_out_account_question_is_gated_and_never_reaches_the_model():
    llm = FakeLLM()
    result, _ = ask(llm, "What is the status of order 8?", customer=None)
    assert result.needs_authentication and result.answer == LOGIN_REQUIRED_MESSAGE
    assert llm.requests == []


def test_answer_without_tools():
    result, _ = ask(FakeLLM(reply("Refunds are available within 14 days.")), "Do you offer refunds?")
    assert result.answer.startswith("Refunds") and result.tools_used == []
    assert result.sources == ["faq.md"]


def test_tool_round_trip_uses_the_authenticated_customer_and_feeds_the_result_back():
    llm = FakeLLM(
        reply(calls=[tool_call("get_order", '{"order_id": 8}')]),
        reply("Order 8 is processing."),
    )
    result, db = ask(llm)
    assert result.answer == "Order 8 is processing." and result.tools_used == ["get_order"]
    assert db.order_lookups == [(8, 1)]  # scoped to Ada's id

    # the second request carried the assistant's call and the tool result
    second_messages, _ = llm.requests[1]
    assert second_messages[-2]["role"] == "assistant" and second_messages[-2]["tool_calls"]
    tool_msg = second_messages[-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1"
    assert json.loads(tool_msg["content"])["order"]["status"] == "processing"


def test_model_supplied_customer_id_is_ignored():
    llm = FakeLLM(
        reply(calls=[tool_call("get_order", '{"order_id": 8, "customer_id": 999, "user": "grace"}')]),
        reply("ok"),
    )
    _, db = ask(llm)
    assert db.order_lookups == [(8, 1)]  # never 999


def test_someone_elses_order_looks_identical_to_a_nonexistent_one():
    tools = tools_for(ADA)
    ctx = ToolContext(db=FakeDB(), customer=ADA)
    others = json.loads(run_tool(tools, "get_order", '{"order_id": 2}', ctx))
    missing = json.loads(run_tool(tools, "get_order", '{"order_id": 999}', ctx))
    assert others["found"] is False
    assert others["message"].replace("2", "N") == missing["message"].replace("999", "N")


def test_signed_out_callers_are_offered_only_public_tools():
    llm = FakeLLM(reply("We sell plans."))
    ask(llm, "What plans do you sell?", customer=None)
    assert llm.requests[0][1] == ["search_products"]


def test_signed_in_callers_are_offered_all_tools():
    llm = FakeLLM(reply("hi"))
    ask(llm, "hello")
    assert set(llm.requests[0][1]) == set(ALL_TOOLS)


def test_account_tool_named_by_a_signed_out_model_is_refused_and_touches_nothing():
    llm = FakeLLM(reply(calls=[tool_call("get_order", '{"order_id": 8}')]), reply("sorry"))
    _, db = ask(llm, "tell me about plans", customer=None)
    assert db.order_lookups == []
    tool_msg = llm.requests[1][0][-1]
    assert "error" in json.loads(tool_msg["content"])


def test_invalid_arguments_become_an_error_result_not_a_crash():
    llm = FakeLLM(reply(calls=[tool_call("get_order", '{"order_id": "abc"}')]), reply("Which order?"))
    result, db = ask(llm)
    assert result.answer == "Which order?" and db.order_lookups == []
    assert "order_id" in json.loads(llm.requests[1][0][-1]["content"])["error"]


def test_numeric_strings_and_missing_or_dict_arguments_are_accepted():
    llm = FakeLLM(
        reply(calls=[tool_call("get_order", '{"order_id": "8"}', "a"), tool_call("list_my_orders", "", "b"),
                     tool_call("list_my_orders", {"limit": 3}, "c")]),
        reply("done"),
    )
    _, db = ask(llm)
    assert db.order_lookups == [(8, 1)]
    results = [json.loads(m["content"]) for m in llm.requests[1][0] if m["role"] == "tool"]
    assert all("error" not in r for r in results)


def test_a_failing_tool_is_reported_to_the_model_without_leaking_details():
    db = FakeDB()
    db.fail = True
    llm = FakeLLM(reply(calls=[tool_call("get_order", '{"order_id": 8}')]), reply("Can't check right now."))
    result, _ = ask(llm, db=db)
    assert result.answer == "Can't check right now."
    content = llm.requests[1][0][-1]["content"]
    assert "exploded" not in content and "error" in json.loads(content)


def test_an_identical_repeated_call_is_not_executed_twice():
    llm = FakeLLM(
        reply(calls=[tool_call("get_order", '{"order_id": 8}', "a")]),
        reply(calls=[tool_call("get_order", '{"order_id": 8}', "b")]),
        reply("Done."),
    )
    result, db = ask(llm)
    assert result.answer == "Done." and db.order_lookups == [(8, 1)]
    assert "already made" in json.loads(llm.requests[2][0][-1]["content"])["note"]


def test_calls_beyond_the_per_step_cap_are_not_executed_but_still_answered():
    calls = [tool_call("get_order", json.dumps({"order_id": n}), f"c{n}") for n in range(1, MAX_CALLS_PER_STEP + 3)]
    llm = FakeLLM(reply(calls=calls), reply("ok"))
    _, db = ask(llm)
    assert len(db.order_lookups) == MAX_CALLS_PER_STEP
    tool_msgs = [m for m in llm.requests[1][0] if m["role"] == "tool"]
    assert len(tool_msgs) == len(calls)  # every call id gets a response


def test_step_cap_forces_a_final_answer_without_tools():
    looping = [reply(calls=[tool_call("get_order", json.dumps({"order_id": n}), f"c{n}")]) for n in range(1, MAX_TOOL_STEPS + 1)]
    llm = FakeLLM(*looping, reply("Here is what I found."))
    result, _ = ask(llm)
    assert result.answer == "Here is what I found."
    assert len(llm.requests) == MAX_TOOL_STEPS + 1
    final_messages, offered = llm.requests[-1]
    assert offered is None and final_messages[-1]["content"] == chat_service.FINAL_ANSWER_NUDGE


def test_empty_model_reply_gets_a_safe_fallback():
    result, _ = ask(FakeLLM(reply("   ")))
    assert result.answer == chat_service.EMPTY_ANSWER_MESSAGE


def test_product_search_receives_cleaned_terms_and_a_decimal_price():
    llm = FakeLLM(reply(calls=[tool_call("search_products", '{"query": "Plans under 10 dollars", "max_price": 10}')]), reply("ok"))
    _, db = ask(llm, "any plan under $10?", customer=None)
    assert db.product_searches == [{"terms": ["plan"], "max_price": Decimal("10.0"), "in_stock_only": False}]


# ---------------------------------------------------------------- the tools


def test_search_terms_cleaning():
    # 2-character tokens are dropped on purpose: numbers belong in max_price,
    # and a "10" keyword would just match noise like "100GB".
    assert search_terms("plans under 10 dollars a month") == ["plan"]
    assert search_terms("Backup drives") == ["backup", "drive"]
    assert search_terms("") == [] and search_terms(None) == []
    assert search_terms("100%_; DROP") == ["100", "drop"]  # only alphanumerics survive
    assert len(search_terms("alpha bravo charlie delta echo foxtrot golf")) == 5


def test_no_tool_schema_exposes_a_customer_identifier():
    for tool in ALL_TOOLS.values():
        schema_text = json.dumps(tool.schema()).lower()
        assert "customer" not in json.dumps(tool.schema()["function"]["parameters"]).lower(), tool.name
        assert tool.schema()["function"]["parameters"]["type"] == "object", schema_text


def test_schemas_are_flat_and_simple_for_small_models():
    for tool in ALL_TOOLS.values():
        text = json.dumps(tool.schema())
        assert '"title"' not in text and "anyOf" not in text and '"null"' not in text, tool.name
    assert ALL_TOOLS["get_order"].schema()["function"]["parameters"]["required"] == ["order_id"]
    assert "required" not in ALL_TOOLS["search_products"].schema()["function"]["parameters"]


def test_oversized_results_are_rejected():
    big = {"x": "y" * 10000}
    tool = ALL_TOOLS["list_my_orders"].__class__(
        "big", "d", ALL_TOOLS["list_my_orders"].args_model, lambda ctx, args: big, requires_login=False
    )
    assert "too large" in json.loads(run_tool({"big": tool}, "big", "{}", ToolContext(FakeDB(), None)))["error"]


# ---------------------------------------------------------------- the LLM wrapper


def http_error(code):
    return HfHubHTTPError(str(code), response=httpx.Response(code, request=httpx.Request("POST", "https://x")))


class FlakyClient:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def llm_with(client, monkeypatch):
    monkeypatch.setattr("app.services.llm.time.sleep", lambda s: None)
    wrapper = LLMClient.__new__(LLMClient)
    wrapper.model, wrapper._client = "m", client
    return wrapper


def test_rate_limit_is_not_retried_and_becomes_503(monkeypatch):
    client = FlakyClient(http_error(429))
    with pytest.raises(ServiceUnavailableError):
        llm_with(client, monkeypatch).complete([])
    assert client.calls == 1


def test_transient_server_error_is_retried_once(monkeypatch):
    client = FlakyClient(http_error(500), "answer")
    assert llm_with(client, monkeypatch).complete([]) == "answer" and client.calls == 2


def test_two_server_errors_become_503_after_one_retry(monkeypatch):
    client = FlakyClient(http_error(500), http_error(500))
    with pytest.raises(ServiceUnavailableError):
        llm_with(client, monkeypatch).complete([])
    assert client.calls == 2


def test_client_errors_such_as_402_or_405_are_not_retried(monkeypatch):
    for code in (402, 405):
        client = FlakyClient(http_error(code))
        with pytest.raises(ServiceUnavailableError):
            llm_with(client, monkeypatch).complete([])
        assert client.calls == 1


def test_network_errors_are_retried(monkeypatch):
    client = FlakyClient(httpx.ConnectError("boom"), "answer")
    assert llm_with(client, monkeypatch).complete([]) == "answer"


def test_tools_are_only_sent_when_provided(monkeypatch):
    seen = []
    client = NS(chat=NS(completions=NS(create=lambda **kw: seen.append(kw) or "r")))
    llm = llm_with(client, monkeypatch)
    llm.complete([], tools=None)
    llm.complete([], tools=[{"type": "function"}])
    assert "tools" not in seen[0] and seen[1]["tool_choice"] == "auto" and seen[1]["temperature"] == 0.2
