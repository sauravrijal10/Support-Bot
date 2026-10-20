"""
app/services/agent.py

The tool-calling agent, built on LangChain's `create_agent` (a LangGraph
graph: model -> tools -> model ... until the model answers without tools).

What is LangChain here, and what deliberately isn't:

* The LOOP, messages, tool calling and the guards are LangChain: the agent
  graph runs the loop, the chat tools are LangChain tools, and the limits
  are agent middleware (StepLimit, ToolCallGuard below).
* The MODEL is our LLMClient (app/services/llm.py) wrapped as a LangChain
  chat model (ClientChatModel), so token rotation, provider fallbacks,
  retries, the 503 mapping, Gemini's thought_signature and streaming with
  "take that back" all keep working exactly as before.
* Tool EXECUTION goes through tools.run_tool. Each LangChain tool carries our
  own JSON schema (so LangChain doesn't validate or reshape arguments) and
  hands the model's raw arguments to run_tool, which validates them, drops
  unknown ones such as an invented `customer_id`, re-checks login, catches
  failures and caps result size. The security model in tools.py is unchanged:
  the customer comes from ToolContext, filled from the verified login token.

An agent is built per request: its tools are bound to that request's
ToolContext (the authenticated customer, the message being answered).
"""

import json
from typing import Any, Callable

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict

from app.services.tools import Tool, ToolContext, _parse_arguments, run_tool

MAX_TOOL_STEPS = 4  # model round-trips that may request tools
MAX_CALLS_PER_STEP = 3  # tool calls honoured in a single round-trip

FINAL_ANSWER_NUDGE = (
    "Answer the customer now using the tool results above. Do not call any more tools."
)
TOO_MANY_CALLS = json.dumps({"error": "Too many tool calls at once; ask for less."})
REPEATED_CALL = json.dumps(
    {"note": "You already made this exact call. Use the earlier result to answer."}
)

# Where the exact wire form of each tool call is kept on an AIMessage, so it
# goes back to the provider unchanged (raw argument text, provider extras).
WIRE_KEY = "tool_call_wire"
# Arguments that weren't a JSON object travel to run_tool under this key, so
# it reports them as invalid instead of LangChain dropping the call.
INVALID_ARGUMENTS = "__invalid_arguments__"


# --------------------------------------------------------------------------
# Messages: LangChain <-> the OpenAI-style dicts LLMClient sends
# --------------------------------------------------------------------------


def _arguments_text(raw) -> str:
    if raw is None or raw == "":
        return "{}"
    return raw if isinstance(raw, str) else json.dumps(raw)


def _to_wire(message: BaseMessage) -> dict:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.content}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": message.content}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    if isinstance(message, AIMessage):
        if not message.tool_calls:
            return {"role": "assistant", "content": message.content or ""}
        wire = message.additional_kwargs.get(WIRE_KEY, {})
        tool_calls = []
        for call in message.tool_calls:
            sent = wire.get(call["id"], {})
            tool_call = {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": sent.get("arguments") or json.dumps(call["args"]),
                },
            }
            # Provider extras must go back untouched: Gemini rejects the next
            # request unless a call still carries its thought_signature.
            if sent.get("extra_content"):
                tool_call["extra_content"] = sent["extra_content"]
            tool_calls.append(tool_call)
        return {"role": "assistant", "content": message.content or "", "tool_calls": tool_calls}
    raise TypeError(f"Unsupported message type: {type(message).__name__}")


def _from_wire(message) -> AIMessage:
    """The provider's reply as an AIMessage. Malformed arguments are kept as
    a normal tool call (see INVALID_ARGUMENTS) so the model gets an error
    result for it, like any other bad call."""
    tool_calls, wire = [], {}
    for i, call in enumerate(message.tool_calls or []):
        call_id = call.id or f"call_{i}"
        raw = call.function.arguments
        try:
            args = _parse_arguments(raw)
        except ValueError:
            args = {INVALID_ARGUMENTS: _arguments_text(raw)}
        tool_calls.append({"name": call.function.name, "args": args, "id": call_id, "type": "tool_call"})
        wire[call_id] = {"arguments": _arguments_text(raw)}
        extra = getattr(call, "extra_content", None)
        if extra:
            wire[call_id]["extra_content"] = extra
    return AIMessage(
        content=message.content or "",
        tool_calls=tool_calls,
        additional_kwargs={WIRE_KEY: wire} if wire else {},
    )


class ClientChatModel(BaseChatModel):
    """LLMClient as a LangChain chat model. `on_text`, if set, receives the
    answer as it is written -- None meaning "drop what was sent so far" (see
    LLMClient.stream); text also goes to LangChain's on_llm_new_token."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_client: Any
    on_text: Callable[[str | None], None] | None = None

    @property
    def _llm_type(self) -> str:
        return "support-bot-llm-client"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        # Our tools carry the exact schema shown to the model; LLMClient always
        # sends tool_choice "auto".
        schemas = [
            t.metadata["schema"] if isinstance(t, BaseTool) and "schema" in (t.metadata or {})
            else convert_to_openai_tool(t)
            for t in tools
        ]
        return self.bind(tools=schemas, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, tools=None, **kwargs) -> ChatResult:
        wire = [_to_wire(m) for m in messages]
        if self.on_text is None:
            response = self.llm_client.complete(wire, tools=tools)
        else:
            def forward(text):
                self.on_text(text)
                if text is not None and run_manager is not None:
                    run_manager.on_llm_new_token(text)

            response = self.llm_client.stream(wire, forward, tools=tools)
        return ChatResult(generations=[ChatGeneration(message=_from_wire(response.choices[0].message))])


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def langchain_tools(tools: dict[str, Tool], ctx: ToolContext) -> list[StructuredTool]:
    """Our tools as LangChain tools, bound to this request's context."""

    def make(name: str, tool: Tool) -> StructuredTool:
        def call(**arguments) -> str:
            raw = arguments.get(INVALID_ARGUMENTS, arguments)
            return run_tool(tools, name, raw, ctx)

        schema = tool.schema()
        return StructuredTool.from_function(
            func=call,
            name=name,
            description=tool.description,
            # A plain JSON schema: LangChain passes arguments through as-is,
            # and run_tool validates them.
            args_schema=schema["function"]["parameters"],
            metadata={"schema": schema},
        )

    return [make(name, tool) for name, tool in tools.items()]


# --------------------------------------------------------------------------
# Guards (agent middleware)
# --------------------------------------------------------------------------


def _this_turn(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Messages after the customer's latest question (earlier turns are
    plain history)."""
    last = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    return messages[last + 1:]


def classify_calls(messages: list[AnyMessage]) -> list[tuple[dict, str]]:
    """Every tool call of this turn with its verdict: "run", "too_many"
    (beyond MAX_CALLS_PER_STEP in one reply) or "repeat" (an identical call
    already ran). Deterministic, so it gives the same answer whichever order
    LangChain runs a reply's calls in."""
    verdicts, seen = [], set()
    for message in _this_turn(messages):
        if not isinstance(message, AIMessage):
            continue
        wire = message.additional_kwargs.get(WIRE_KEY, {})
        for index, call in enumerate(message.tool_calls):
            key = (call["name"], wire.get(call["id"], {}).get("arguments") or json.dumps(call["args"]))
            if index >= MAX_CALLS_PER_STEP:
                verdicts.append((call, "too_many"))
            elif key in seen:
                verdicts.append((call, "repeat"))
            else:
                seen.add(key)
                verdicts.append((call, "run"))
    return verdicts


class StepLimit(AgentMiddleware):
    """At most MAX_TOOL_STEPS model calls may request tools. After that the
    model is asked for a plain answer from what it has, with no tools
    offered (any tool calls it invents anyway are dropped)."""

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        steps = sum(isinstance(m, AIMessage) for m in _this_turn(request.messages))
        if steps < MAX_TOOL_STEPS:
            return handler(request)
        response = handler(request.override(
            tools=[], messages=[*request.messages, HumanMessage(FINAL_ANSWER_NUDGE)]
        ))
        answer = [
            AIMessage(content=m.content) if isinstance(m, AIMessage) else m for m in response.result
        ]
        return ModelResponse(result=answer, structured_response=response.structured_response)


class ToolCallGuard(AgentMiddleware):
    """Calls beyond MAX_CALLS_PER_STEP in one reply, and exact repeats of a
    call already made this turn, are answered without running the tool.
    A tool the caller wasn't offered (e.g. an account tool named for a
    signed-out customer) gets run_tool's refusal. Every call still gets a
    result, which providers require."""

    def __init__(self, tools: dict[str, Tool], ctx: ToolContext):
        super().__init__()
        # Not `self.tools`: on middleware that name registers extra tools.
        self._offered, self._ctx = tools, ctx

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] not in self._offered:
            content = run_tool(self._offered, call["name"], call["args"], self._ctx)
            return ToolMessage(content=content, tool_call_id=call["id"], name=call["name"])
        verdict = next(
            (v for c, v in classify_calls(request.state["messages"]) if c["id"] == call["id"]), "run"
        )
        if verdict == "run":
            return handler(request)
        content = TOO_MANY_CALLS if verdict == "too_many" else REPEATED_CALL
        return ToolMessage(content=content, tool_call_id=call["id"], name=call["name"])


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


def run_agent(
    llm, tools: dict[str, Tool], ctx: ToolContext, messages: list[BaseMessage], on_text=None
) -> tuple[str, list[str]]:
    """Run the agent over `messages` (system prompt, history, question).
    Returns (the final answer text, names of the tools that actually ran)."""
    agent = create_agent(
        model=ClientChatModel(llm_client=llm, on_text=on_text),
        tools=langchain_tools(tools, ctx),
        middleware=[StepLimit(), ToolCallGuard(tools, ctx)],
    )
    state = agent.invoke({"messages": messages})
    final = state["messages"][-1]
    used = [call["name"] for call, verdict in classify_calls(state["messages"]) if verdict == "run"]
    return (final.content if isinstance(final, AIMessage) else ""), used
