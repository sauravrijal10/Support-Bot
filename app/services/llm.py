"""
app/services/llm.py

Thin wrapper around the Hugging Face chat client. It owns the model name,
provider and error handling, so the rest of the code just calls
`llm.complete(messages, tools=...)`.

The free/cheap tiers are flaky, so it retries once on a transient
server/network error and turns every provider failure into a clean 503
instead of an unhandled 500.

Several HF tokens can be configured (HF_TOKEN + HF_FALLBACK_TOKENS). When a
token fails for an account-level reason -- expired/invalid (401), out of
credits (402), not permitted (403) or rate-limited (429) -- the same request
is retried with the next token, and the failed one is skipped for a while
(ACCOUNT_COOLDOWNS) so later requests don't hit it first. Rate limits are
never retried on the same token; that only makes them worse.

Other OpenAI-compatible providers (Groq, Gemini, ... -- see FALLBACK_PROVIDERS
in app/config.py) can be configured as well. They come after the HF tokens,
each with its own model, and are also used when Hugging Face itself is failing
(a 5xx or a rejected request), which no other HF token would fix.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
from huggingface_hub import ChatCompletionOutput, ChatCompletionStreamOutput, InferenceClient
from huggingface_hub.errors import HfHubHTTPError

from app.services.errors import ServiceUnavailableError

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 500
# Low temperature: tool arguments and "answer only from the data" both want
# consistent behaviour more than creativity.
TEMPERATURE = 0.2

# Seconds a token is skipped after an account-level failure. After that it
# is tried again (credits may have been topped up, a limit may have reset).
ACCOUNT_COOLDOWNS = {401: 3600, 402: 3600, 403: 3600, 429: 60}

FALLBACK_TIMEOUT = 60  # seconds

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again shortly."
BUSY = "The assistant is receiving too many requests right now. Please try again in a moment."


@dataclass
class _Token:
    label: str  # for logs only, e.g. "token 2 (...ab12)" -- never the token itself
    client: object
    model: str
    provider: str = "huggingface"
    unusable_until: float = 0.0


class _AccountError(Exception):
    def __init__(self, code: int):
        self.code = code


class _ProviderError(Exception):
    """The provider itself failed; its other tokens would fail the same way."""


class _OpenAICompatibleClient:
    """`client.chat.completions.create(...)` for any OpenAI-compatible URL,
    failing and answering the way InferenceClient does. InferenceClient can
    post to such a URL itself, but crashes on error bodies it doesn't know
    (Gemini's is a JSON list), which would escape the handling below."""

    def __init__(self, url: str, api_key: str):
        self._url = url
        self._http = httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=FALLBACK_TIMEOUT)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, stream: bool = False, **payload):
        if stream:
            return self._stream(payload)
        response = self._http.post(self._url, json=payload)
        if response.is_error:
            raise HfHubHTTPError(response.text, response=response)
        return ChatCompletionOutput.parse_obj_as_instance(response.json())

    def _stream(self, payload: dict):
        """The answer as chunks, read from the server-sent events."""
        with self._http.stream("POST", self._url, json={**payload, "stream": True}) as response:
            if response.is_error:
                response.read()
                raise HfHubHTTPError(response.text, response=response)
            for line in response.iter_lines():
                data = line[len("data:"):].strip() if line.startswith("data:") else ""
                if data == "[DONE]":
                    return
                if data:
                    yield ChatCompletionStreamOutput.parse_obj_as_instance(json.loads(data))


def _read_stream(chunks, on_text):
    """Play a streamed answer through on_text and return it whole, shaped
    like a non-streamed response. Text that turns out to precede tool calls
    ("Let me check...") is not the answer, so it is taken back."""
    text, calls, sent = [], {}, False
    for chunk in chunks:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            text.append(delta.content)
            on_text(delta.content)
            sent = True
        for part in delta.tool_calls or []:
            # Providers that send a call in one piece may leave the index out.
            index = part.index if part.index is not None else len(calls)
            call = calls.setdefault(
                index, SimpleNamespace(id=None, function=SimpleNamespace(name="", arguments=""))
            )
            call.id = part.id or call.id
            function = part.function
            if function:
                call.function.name += function.name or ""
                call.function.arguments += function.arguments or ""
            extra = getattr(part, "extra_content", None)
            if extra:
                call.extra_content = extra
    if calls and sent:
        on_text(None)
    message = SimpleNamespace(content="".join(text) or None, tool_calls=list(calls.values()) or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class LLMClient:
    def __init__(self, api_keys: str | list[str], model: str, provider: str = "auto", fallbacks=()):
        """`fallbacks` are app.config.FallbackLLM entries, tried in order after the HF tokens."""
        keys = [api_keys] if isinstance(api_keys, str) else list(api_keys)
        if not keys:
            raise ValueError("at least one Hugging Face token is required")
        self.model = model
        self._tokens = [
            _Token(f"token {i} (...{key[-4:]})", InferenceClient(api_key=key, provider=provider), model)
            for i, key in enumerate(keys, start=1)
        ]
        for fallback in fallbacks:
            for key in fallback.api_keys:
                client = _OpenAICompatibleClient(fallback.url, key)
                self._tokens.append(
                    _Token(f"{fallback.name} (...{key[-4:]})", client, fallback.model, fallback.name)
                )
        self._lock = threading.Lock()  # complete() runs in FastAPI's threadpool

    @classmethod
    def with_clients(cls, model: str, clients: list, fallbacks: dict | None = None) -> "LLMClient":
        """Build around ready-made chat clients (used by the tests).
        `fallbacks` maps a provider name to its client."""
        llm = cls.__new__(cls)
        llm.model = model
        llm._tokens = [_Token(f"token {i}", c, model) for i, c in enumerate(clients, start=1)]
        llm._tokens += [_Token(name, c, f"{name}-model", name) for name, c in (fallbacks or {}).items()]
        llm._lock = threading.Lock()
        return llm

    def _in_order(self) -> list[_Token]:
        """Usable tokens in configured order. If all are cooling down, try
        them all anyway, the soonest-available first -- better than refusing."""
        now = time.monotonic()
        with self._lock:
            usable = [t for t in self._tokens if t.unusable_until <= now]
            return usable or sorted(self._tokens, key=lambda t: t.unusable_until)

    def _bench(self, token: _Token, code: int) -> None:
        with self._lock:
            token.unusable_until = time.monotonic() + ACCOUNT_COOLDOWNS[code]

    def complete(self, messages: list[dict], tools: list[dict] | None = None):
        """One chat-completion call. Returns the provider's response object."""
        return self._request(messages, tools, None)

    def stream(self, messages: list[dict], on_text, tools: list[dict] | None = None):
        """complete(), but the answer's text is also passed to on_text(str)
        piece by piece as it is written. on_text(None) means "forget what was
        sent so far": that text preceded tool calls, or the provider failed
        part-way and another one is starting over."""
        return self._request(messages, tools, on_text)

    def _request(self, messages, tools, on_text):
        kwargs = {
            "messages": messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        codes, down = [], set()
        for token in self._in_order():
            if token.provider in down:
                continue
            try:
                return self._complete_with(token, kwargs, on_text)
            except _AccountError as error:
                self._bench(token, error.code)
                codes.append(error.code)
                logger.warning(
                    "LLM %s failed with %s; skipping it for %ss and trying the next token",
                    token.label, error.code, ACCOUNT_COOLDOWNS[error.code],
                )
            except _ProviderError:
                down.add(token.provider)
        # Every token failed.
        rate_limited = codes and not down and all(c == 429 for c in codes)
        raise ServiceUnavailableError(BUSY if rate_limited else UNAVAILABLE)

    @staticmethod
    def _call(token: _Token, kwargs: dict, on_text):
        create = token.client.chat.completions.create
        if on_text is None:
            return create(model=token.model, **kwargs)

        sent = []

        def forward(text):
            sent.append(text)
            on_text(text)

        try:
            return _read_stream(create(model=token.model, stream=True, **kwargs), forward)
        except (HfHubHTTPError, httpx.TransportError):
            # Whoever answers next starts the answer again from scratch.
            if sent and sent[-1] is not None:
                on_text(None)
            raise

    def _complete_with(self, token: _Token, kwargs: dict, on_text=None):
        """Call one token, retrying once on a transient failure. Raises
        _AccountError for failures another token might not have, and
        _ProviderError for those only another provider might not have."""
        for attempt in (1, 2):
            try:
                return self._call(token, kwargs, on_text)
            except HfHubHTTPError as error:
                code = getattr(error.response, "status_code", None)
                # Customers only see a generic 503, so keep the real cause
                # (e.g. 402 = HF credits used up) in the server log.
                logger.warning("LLM provider error %s on %s: %s", code, token.label, str(error)[:500])
                if code in ACCOUNT_COOLDOWNS:
                    raise _AccountError(code) from error
                transient = code is None or code >= 500
            except httpx.TransportError as error:
                logger.warning("LLM provider unreachable on %s: %s", token.label, error)
                transient = True
            if not transient or attempt == 2:
                raise _ProviderError()
            time.sleep(1)
