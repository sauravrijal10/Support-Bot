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
from app.services.tools import ALL_TOOLS, CART_NOTE, ToolContext, run_tool, search_terms, tools_for

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
        self.drafts = []
        self.carts = []
        self.confirms = []
        self.cancel_requests = []
        self.cancel_confirms = []
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

    def search_products(self, terms, category=None, max_price=None, in_stock_only=False, limit=10):
        self.product_searches.append(
            {"terms": terms, "category": category, "max_price": max_price, "in_stock_only": in_stock_only}
        )
        return [{"sku": "PLUS-PLAN", "name": "Plus plan", "type": "Digital services", "description": "100GB",
                 "attributes": {"Storage": "100 GB"}, "price": Decimal("5.00"),
                 "billing_period": "monthly", "stock_quantity": None}], 1

    def prepare_order_draft(self, customer_id, lines, now):
        self.drafts.append((customer_id, lines))
        return {"draft_ready": True, "total": "48.00", "form": {"draft_id": 7, "lines": [], "total": "48.00"}}

    def prepare_cart(self, customer_id, skus, now):
        self.carts.append((customer_id, skus))
        if self.fail:
            raise RuntimeError("db exploded")
        return {"kind": "cart", "draft_id": 11, "lines": [], "total": "0.00"}

    def confirm_order_draft(self, customer_id, request_started_at, now):
        self.confirms.append((customer_id, request_started_at))
        return {"placed": True, "order": {"order_id": 9}}

    def request_order_cancellation(self, customer_id, order_id, now):
        self.cancel_requests.append((customer_id, order_id))
        return {"cancellable": True, "order": {"order_id": order_id}}

    def confirm_order_cancellation(self, customer_id, request_started_at, now):
        self.cancel_confirms.append(customer_id)
        return {"cancelled": True}

    def list_product_types(self):
        return [{"slug": "books", "name": "Books", "description": "Printed books and e-books.",
                 "attribute_schema": [{"key": "author", "label": "Author"}, {"key": "format"}],
                 "product_count": 4}]


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
    assert llm.requests[0][1] == ["search_products", "list_product_types"]


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


@pytest.mark.parametrize("raw", ['{"order_id": 8', "[8]", '"8"'])
def test_malformed_arguments_become_an_error_result_and_run_nothing(raw):
    llm = FakeLLM(reply(calls=[tool_call("get_order", raw)]), reply("Which order?"))
    result, db = ask(llm)
    assert result.answer == "Which order?" and db.order_lookups == []
    assert "Invalid arguments" in json.loads(llm.requests[1][0][-1]["content"])["error"]
    # The call goes back to the provider exactly as the model wrote it.
    sent_back = next(m for m in llm.requests[1][0] if m["role"] == "assistant")["tool_calls"][0]
    assert sent_back["function"]["arguments"] == raw


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
    assert db.product_searches == [
        {"terms": ["plan"], "category": None, "max_price": Decimal("10.0"), "in_stock_only": False}
    ]


def test_product_search_passes_a_normalised_category_and_shows_type_and_details():
    llm = FakeLLM(reply(calls=[tool_call("search_products", '{"category": "  Books "}')]), reply("ok"))
    _, db = ask(llm, "what books do you have?", customer=None)
    assert db.product_searches[0]["category"] == "books"

    result = json.loads(llm.requests[1][0][-1]["content"])["products"][0]
    assert result["type"] == "Digital services"
    assert result["details"] == {"Storage": "100 GB"}


def test_in_stock_search_still_names_sold_out_matches():
    class StockDB(FakeDB):
        def search_products(self, terms, category=None, max_price=None, in_stock_only=False, limit=10):
            self.product_searches.append({"in_stock_only": in_stock_only})
            hub = {"sku": "HUB", "name": "USB-C hub", "type": "Electronics", "description": "",
                   "attributes": {}, "price": Decimal("49.00"), "billing_period": "one_time",
                   "stock_quantity": 0}
            return ([], 0) if in_stock_only else ([hub], 1)

    llm = FakeLLM(reply(calls=[tool_call("search_products", '{"query": "usb hub", "in_stock_only": true}')]),
                  reply("Sold out."))
    _, db = ask(llm, "Is the USB-C hub available?", customer=None, db=StockDB())
    result = json.loads(llm.requests[1][0][-1]["content"])
    assert result["products"] == [] and result["sold_out_matches"] == ["USB-C hub"]
    assert db.product_searches == [{"in_stock_only": True}, {"in_stock_only": False}]


def test_list_product_types_is_public_and_shows_categories():
    llm = FakeLLM(reply(calls=[tool_call("list_product_types", "{}")]), reply("We sell books."))
    result, _ = ask(llm, "what do you sell?", customer=None)
    content = json.loads(llm.requests[1][0][-1]["content"])
    assert content["product_types"] == [
        {"category": "books", "name": "Books", "description": "Printed books and e-books.",
         "products": 4, "details_listed": ["Author", "format"]}
    ]
    assert result.tools_used == ["list_product_types"]


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


def test_provider_extras_on_a_tool_call_are_sent_back_with_it():
    # Gemini rejects the follow-up request unless its thought_signature is returned.
    extra = {"google": {"thought_signature": "sig"}}
    signed = NS(id="call_1", function=NS(name="list_product_types", arguments="{}"), extra_content=extra)
    llm = FakeLLM(reply(calls=[signed, tool_call("search_products", "{}", "call_2")]), reply("done"))
    ask(llm, "What do you sell?")
    sent_back = next(m for m in llm.requests[1][0] if m["role"] == "assistant")["tool_calls"]
    assert sent_back[0]["extra_content"] == extra and "extra_content" not in sent_back[1]


def test_schemas_are_flat_and_simple_for_small_models():
    for tool in ALL_TOOLS.values():
        text = json.dumps(tool.schema())
        assert '"title"' not in text and "anyOf" not in text and '"null"' not in text, tool.name
        assert "$ref" not in text and "$defs" not in text, tool.name
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


def llm_with(client, monkeypatch, *more_clients):
    monkeypatch.setattr("app.services.llm.time.sleep", lambda s: None)
    return LLMClient.with_clients("m", [client, *more_clients])


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


@pytest.mark.parametrize("code", [401, 402, 403, 429])
def test_an_account_level_failure_moves_on_to_the_next_token(monkeypatch, code):
    first, second = FlakyClient(http_error(code)), FlakyClient("answer")
    assert llm_with(first, monkeypatch, second).complete([]) == "answer"
    assert first.calls == 1 and second.calls == 1


def test_a_failed_token_is_skipped_on_later_requests(monkeypatch):
    first, second = FlakyClient(http_error(402)), FlakyClient("one", "two")
    llm = llm_with(first, monkeypatch, second)
    assert llm.complete([]) == "one" and llm.complete([]) == "two"
    assert first.calls == 1  # not tried again while cooling down


def test_a_token_is_tried_again_once_its_cooldown_is_over(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.services.llm.time.monotonic", lambda: clock[0])
    first, second = FlakyClient(http_error(402), "back"), FlakyClient("fallback")
    llm = llm_with(first, monkeypatch, second)
    assert llm.complete([]) == "fallback"
    clock[0] += 3601
    assert llm.complete([]) == "back"  # the first token is preferred again


def test_when_every_token_is_benched_they_are_still_tried(monkeypatch):
    first, second = FlakyClient(http_error(402), "refilled"), FlakyClient(http_error(402))
    llm = llm_with(first, monkeypatch, second)
    with pytest.raises(ServiceUnavailableError):
        llm.complete([])
    assert llm.complete([]) == "refilled"


def test_all_tokens_rate_limited_says_busy_otherwise_unavailable(monkeypatch):
    with pytest.raises(ServiceUnavailableError, match="too many requests"):
        llm_with(FlakyClient(http_error(429)), monkeypatch, FlakyClient(http_error(429))).complete([])
    with pytest.raises(ServiceUnavailableError, match="temporarily unavailable"):
        llm_with(FlakyClient(http_error(402)), monkeypatch, FlakyClient(http_error(429))).complete([])


@pytest.mark.parametrize("error", [http_error(500), http_error(405)])
def test_provider_side_failures_do_not_switch_tokens(monkeypatch, error):
    # A provider outage or a bad request fails the same way with any token.
    first, second = FlakyClient(error, error), FlakyClient("unused")
    with pytest.raises(ServiceUnavailableError):
        llm_with(first, monkeypatch, second).complete([])
    assert second.calls == 0


def test_tokens_from_settings_are_ordered_and_deduplicated(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("HF_TOKEN", "hf_main")
    monkeypatch.setenv("HF_FALLBACK_TOKENS", " hf_second, ,hf_main,hf_third ")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("JWT_SECRET", "s" * 40)
    get_settings.cache_clear()
    try:
        assert get_settings().hf_tokens == ("hf_main", "hf_second", "hf_third")
    finally:
        get_settings.cache_clear()


def test_other_providers_come_after_the_tokens_with_their_own_model(monkeypatch):
    seen = []
    groq = NS(chat=NS(completions=NS(create=lambda **kw: seen.append(kw["model"]) or "from groq")))
    first, second = FlakyClient(http_error(402)), FlakyClient(http_error(402))
    monkeypatch.setattr("app.services.llm.time.sleep", lambda s: None)
    llm = LLMClient.with_clients("m", [first, second], {"groq": groq})
    assert llm.complete([]) == "from groq" and seen == ["groq-model"]


@pytest.mark.parametrize("error", [http_error(500), http_error(404), httpx.ConnectError("boom")])
def test_a_provider_side_failure_moves_on_to_another_provider(monkeypatch, error):
    monkeypatch.setattr("app.services.llm.time.sleep", lambda s: None)
    first, second, groq = FlakyClient(error, error), FlakyClient("unused"), FlakyClient("from groq")
    llm = LLMClient.with_clients("m", [first, second], {"groq": groq})
    assert llm.complete([]) == "from groq" and second.calls == 0


def test_the_openai_compatible_client_parses_answers_and_raises_like_the_hf_one():
    from app.services.llm import _OpenAICompatibleClient

    answer = {"choices": [{"message": {"role": "assistant", "content": "hi", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "get_order", "arguments": "{}"}}]}}]}
    replies = [httpx.Response(200, json=answer), httpx.Response(429, json=[{"error": {"code": 429}}])]
    client = _OpenAICompatibleClient("https://x/chat/completions", "key")
    client._http = httpx.Client(transport=httpx.MockTransport(lambda request: replies.pop(0)))
    message = client.chat.completions.create(model="m", messages=[]).choices[0].message
    assert message.content == "hi" and message.tool_calls[0].function.name == "get_order"
    with pytest.raises(HfHubHTTPError) as raised:
        client.chat.completions.create(model="m", messages=[])
    assert raised.value.response.status_code == 429


def chunk(content=None, calls=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=calls))])


def call_part(index, call_id=None, name=None, arguments=None, **extra):
    return NS(index=index, id=call_id, function=NS(name=name, arguments=arguments), **extra)


class StreamingClient(FlakyClient):
    """Each outcome is an error to raise or a list of chunks; an error inside
    the list is raised part-way through the stream."""

    def _create(self, stream=False, **kwargs):
        outcome = super()._create(**kwargs)
        assert stream

        def chunks():
            for item in outcome:
                if isinstance(item, Exception):
                    raise item
                yield item

        return chunks()


def test_streaming_passes_text_on_as_it_arrives_and_returns_the_whole_answer(monkeypatch):
    seen = []
    client = StreamingClient([chunk("Hel"), chunk("lo"), NS(choices=[])])
    message = llm_with(client, monkeypatch).stream([], seen.append).choices[0].message
    assert seen == ["Hel", "lo"] and message.content == "Hello" and message.tool_calls is None


def test_streamed_tool_calls_are_put_together_and_text_before_them_is_taken_back(monkeypatch):
    seen = []
    extra = {"google": {"thought_signature": "sig"}}
    client = StreamingClient([
        chunk("Let me check"),
        chunk(calls=[call_part(0, "c1", "get_order", '{"order'), call_part(1, "c2", "list_my_orders", "{}")]),
        chunk(calls=[call_part(0, arguments='_id": 8}', extra_content=extra)]),
    ])
    first, second = llm_with(client, monkeypatch).stream([], seen.append).choices[0].message.tool_calls
    assert (first.id, first.function.name, first.function.arguments) == ("c1", "get_order", '{"order_id": 8}')
    assert first.extra_content == extra and second.function.name == "list_my_orders"
    assert seen == ["Let me check", None]


def test_a_stream_that_breaks_part_way_starts_over_on_another_provider(monkeypatch):
    seen = []
    broken = [chunk("Hel"), httpx.ReadError("boom")]
    first, groq = StreamingClient(broken, broken), StreamingClient([chunk("Hi")])
    monkeypatch.setattr("app.services.llm.time.sleep", lambda s: None)
    llm = LLMClient.with_clients("m", [first], {"groq": groq})
    assert llm.stream([], seen.append).choices[0].message.content == "Hi"
    assert seen == ["Hel", None, "Hel", None, "Hi"]


def test_the_openai_compatible_client_reads_a_stream_and_its_errors():
    from app.services.llm import _OpenAICompatibleClient

    events = (
        'data: {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}}]}\n\n'
        ': keep-alive\n\n'
        'data: {"choices": [{"index": 0, "delta": {"tool_calls": [{"function": {"name": "get_order", '
        '"arguments": "{}"}, "id": "c1", "type": "function", "extra_content": {"google": {}}}]}}]}\n\n'
        'data: [DONE]\n\n'
    )
    replies = [httpx.Response(200, text=events), httpx.Response(429, json=[{"error": {"code": 429}}])]
    client = _OpenAICompatibleClient("https://x/chat/completions", "key")
    client._http = httpx.Client(transport=httpx.MockTransport(lambda request: replies.pop(0)))
    text, calls = list(client.chat.completions.create(model="m", messages=[], stream=True))
    assert text.choices[0].delta.content == "Hi"
    assert calls.choices[0].delta.tool_calls[0].function.name == "get_order"
    with pytest.raises(HfHubHTTPError) as raised:
        list(client.chat.completions.create(model="m", messages=[], stream=True))
    assert raised.value.response.status_code == 429


def test_fallback_providers_from_settings(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("HF_TOKEN", "hf_main")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("JWT_SECRET", "s" * 40)
    for name in ("GROQ", "GEMINI", "CEREBRAS", "OPENROUTER"):
        monkeypatch.delenv(f"{name}_API_KEY", raising=False)
        monkeypatch.delenv(f"{name}_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or_key")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_a, gsk_b")
    monkeypatch.setenv("GROQ_MODEL", "some-model")
    get_settings.cache_clear()
    try:
        groq, openrouter = get_settings().llm_fallbacks
        assert (groq.name, groq.model, groq.api_keys) == ("groq", "some-model", ("gsk_a", "gsk_b"))
        assert openrouter.name == "openrouter" and openrouter.model.endswith(":free")
        llm = LLMClient("hf_main", "m", "deepinfra", get_settings().llm_fallbacks)
        assert [t.provider for t in llm._tokens] == ["huggingface", "groq", "groq", "openrouter"]
    finally:
        get_settings.cache_clear()


def test_tools_are_only_sent_when_provided(monkeypatch):
    seen = []
    client = NS(chat=NS(completions=NS(create=lambda **kw: seen.append(kw) or "r")))
    llm = llm_with(client, monkeypatch)
    llm.complete([], tools=None)
    llm.complete([], tools=[{"type": "function"}])
    assert "tools" not in seen[0] and seen[1]["tool_choice"] == "auto" and seen[1]["temperature"] == 0.2


# ---------------------------------------------------------------- ordering


WRITE_TOOLS = {"prepare_order", "confirm_order", "request_cancellation", "confirm_cancellation"}


def test_ordering_and_cancelling_tools_are_never_offered_to_signed_out_callers():
    assert not WRITE_TOOLS & set(tools_for(None))
    assert WRITE_TOOLS <= set(tools_for(ADA))


def test_prepare_order_schema_is_inlined_and_needs_items():
    params = ALL_TOOLS["prepare_order"].schema()["function"]["parameters"]
    assert params["required"] == ["items"]
    line = params["properties"]["items"]["items"]
    assert line["type"] == "object" and set(line["properties"]) == {"sku", "quantity"}


def test_prepare_order_quotes_for_the_authenticated_customer_only():
    args = '{"items": [{"sku": "TSHIRT-LOGO-BLK-M", "quantity": 2}], "customer_id": 2}'
    llm = FakeLLM(reply(calls=[tool_call("prepare_order", args)]), reply("That's $48. Confirm?"))
    result, db = ask(llm, "I'd like two black T-shirts in M")
    assert db.drafts == [(ADA["id"], [("TSHIRT-LOGO-BLK-M", 2)])]
    assert db.confirms == []  # quoting never places anything
    assert result.tools_used == ["prepare_order"]
    # The form goes to the customer's screen, not into the model's context.
    assert result.order_form == {"draft_id": 7, "lines": [], "total": "48.00"}
    assert "form" not in json.loads(llm.requests[1][0][-1]["content"])


@pytest.mark.parametrize("args", ['{"items": []}', '{"items": [{"sku": "X", "quantity": 0}]}', "{}"])
def test_prepare_order_rejects_bad_lines(args):
    llm = FakeLLM(reply(calls=[tool_call("prepare_order", args)]), reply("hmm"))
    _, db = ask(llm, "order something")
    assert db.drafts == [] and "error" in json.loads(llm.requests[1][0][-1]["content"])


def test_confirm_order_places_the_order_when_the_customer_says_yes():
    llm = FakeLLM(reply(calls=[tool_call("confirm_order", "{}")]), reply("Done! Order 9."))
    _, db = ask(llm, "Yes, place it please")
    assert len(db.confirms) == 1 and db.confirms[0][0] == ADA["id"]
    assert json.loads(llm.requests[1][0][-1]["content"])["placed"] is True


@pytest.mark.parametrize("message", ["How much is the hoodie?", "No, wait", "yes but change the size", "hmm"])
def test_confirm_order_is_refused_unless_the_message_is_a_clear_yes(message):
    llm = FakeLLM(reply(calls=[tool_call("confirm_order", "{}")]), reply("Shall I place it?"))
    _, db = ask(llm, message)
    assert db.confirms == []  # the database was never asked to place anything
    assert json.loads(llm.requests[1][0][-1]["content"])["placed"] is False


def test_confirm_passes_when_this_request_started():
    llm = FakeLLM(reply(calls=[tool_call("confirm_order", "{}")]), reply("Done."))
    before = datetime.now().astimezone()
    _, db = ask(llm, "yes")
    assert before <= db.confirms[0][1] <= datetime.now().astimezone()


# ---------------------------------------------------------------- cancelling


def test_request_cancellation_is_scoped_to_the_authenticated_customer():
    args = '{"order_id": 8, "customer_id": 2}'
    llm = FakeLLM(reply(calls=[tool_call("request_cancellation", args)]), reply("Cancel order 8?"))
    _, db = ask(llm, "Please cancel order 8")
    assert db.cancel_requests == [(ADA["id"], 8)] and db.cancel_confirms == []


def test_confirm_cancellation_runs_when_the_customer_says_yes_cancel_it():
    llm = FakeLLM(reply(calls=[tool_call("confirm_cancellation", "{}")]), reply("Cancelled."))
    _, db = ask(llm, "Yes, cancel it")
    assert db.cancel_confirms == [ADA["id"]]


@pytest.mark.parametrize("message", ["no, keep it", "What's in order 8?", "wait"])
def test_confirm_cancellation_is_refused_unless_the_message_is_a_clear_yes(message):
    llm = FakeLLM(reply(calls=[tool_call("confirm_cancellation", "{}")]), reply("Keep it?"))
    _, db = ask(llm, message)
    assert db.cancel_confirms == []
    assert json.loads(llm.requests[1][0][-1]["content"])["cancelled"] is False


def test_a_form_with_an_empty_reply_gets_a_sentence_pointing_at_it():
    llm = FakeLLM(reply(calls=[tool_call("prepare_order", '{"items": [{"sku": "HOODIE"}]}')]), reply("[Order Form]"))
    result, _ = ask(llm, "I'd like a hoodie")
    assert result.answer == chat_service.ORDER_FORM_MESSAGE and result.order_form is not None


# ---------------------------------------------------------------- the cart


SEARCH = tool_call("search_products", '{"query": "plan"}')


def test_products_shown_to_a_signed_in_customer_come_with_a_cart():
    llm = FakeLLM(reply(calls=[SEARCH]), reply("We have the Plus plan -- see the cart below."))
    result, db = ask(llm, "Which plans do you sell?")
    assert db.carts == [(ADA["id"], ["PLUS-PLAN"])]
    assert result.order_form["kind"] == "cart"
    # The model is told the customer sees a cart, so it doesn't list everything again.
    assert json.loads(llm.requests[1][0][-1]["content"])["display"] == CART_NOTE


def test_signed_out_callers_get_no_cart():
    llm = FakeLLM(reply(calls=[SEARCH]), reply("We have the Plus plan."))
    result, db = ask(llm, "Which plans do you sell?", customer=None)
    assert db.carts == [] and result.order_form is None
    assert "display" not in json.loads(llm.requests[1][0][-1]["content"])


def test_an_order_form_is_not_replaced_by_a_cart():
    prepare = tool_call("prepare_order", '{"items": [{"sku": "PLUS-PLAN", "quantity": 1}]}', "call_2")
    llm = FakeLLM(reply(calls=[SEARCH]), reply(calls=[prepare]), reply("Review and confirm the form."))
    result, db = ask(llm, "I'd like the Plus plan")
    assert db.carts == [] and result.order_form["draft_id"] == 7


def test_no_cart_while_confirming_or_cancelling():
    confirm = tool_call("confirm_order", "{}", "call_2")
    llm = FakeLLM(reply(calls=[SEARCH, confirm]), reply("Done! Order 9."))
    result, db = ask(llm, "Yes, place it please")
    assert db.carts == [] and result.order_form is None


def test_a_failing_cart_does_not_lose_the_answer():
    db = FakeDB()
    llm = FakeLLM(reply(calls=[SEARCH]), reply("We have the Plus plan."))
    db.fail = True
    result, _ = ask(llm, "Which plans do you sell?", db=db)
    assert result.answer == "We have the Plus plan." and result.order_form is None


def test_a_cart_with_an_empty_reply_gets_a_sentence_pointing_at_it():
    llm = FakeLLM(reply(calls=[SEARCH]), reply(""))
    result, _ = ask(llm, "Which plans do you sell?")
    assert result.answer == chat_service.CART_MESSAGE
