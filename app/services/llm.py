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
"""

import logging
import threading
import time
from dataclasses import dataclass

import httpx
from huggingface_hub import InferenceClient
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

UNAVAILABLE = "The assistant is temporarily unavailable. Please try again shortly."
BUSY = "The assistant is receiving too many requests right now. Please try again in a moment."


@dataclass
class _Token:
    label: str  # for logs only, e.g. "token 2 (...ab12)" -- never the token itself
    client: object
    unusable_until: float = 0.0


class _AccountError(Exception):
    def __init__(self, code: int):
        self.code = code


class LLMClient:
    def __init__(self, api_keys: str | list[str], model: str, provider: str = "auto"):
        keys = [api_keys] if isinstance(api_keys, str) else list(api_keys)
        if not keys:
            raise ValueError("at least one Hugging Face token is required")
        self.model = model
        self._tokens = [
            _Token(f"token {i} (...{key[-4:]})", InferenceClient(api_key=key, provider=provider))
            for i, key in enumerate(keys, start=1)
        ]
        self._lock = threading.Lock()  # complete() runs in FastAPI's threadpool

    @classmethod
    def with_clients(cls, model: str, clients: list) -> "LLMClient":
        """Build around ready-made chat clients (used by the tests)."""
        llm = cls.__new__(cls)
        llm.model = model
        llm._tokens = [_Token(f"token {i}", c) for i, c in enumerate(clients, start=1)]
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
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        codes = []
        for token in self._in_order():
            try:
                return self._complete_with(token, kwargs)
            except _AccountError as error:
                self._bench(token, error.code)
                codes.append(error.code)
                logger.warning(
                    "LLM %s failed with %s; skipping it for %ss and trying the next token",
                    token.label, error.code, ACCOUNT_COOLDOWNS[error.code],
                )
        # Every token failed at account level.
        raise ServiceUnavailableError(BUSY if codes and all(c == 429 for c in codes) else UNAVAILABLE)

    def _complete_with(self, token: _Token, kwargs: dict):
        """Call one token, retrying once on a transient failure. Raises
        _AccountError for failures another token might not have."""
        for attempt in (1, 2):
            try:
                return token.client.chat.completions.create(**kwargs)
            except HfHubHTTPError as error:
                code = getattr(error.response, "status_code", None)
                # Customers only see a generic 503, so keep the real cause
                # (e.g. 402 = HF credits used up) in the server log.
                logger.warning("LLM provider error %s on %s: %s", code, token.label, str(error)[:500])
                if code in ACCOUNT_COOLDOWNS:
                    raise _AccountError(code) from error
                transient = code is None or code >= 500
            except httpx.TransportError as error:
                logger.warning("LLM provider unreachable: %s", error)
                transient = True
            if not transient or attempt == 2:
                raise ServiceUnavailableError(UNAVAILABLE)
            time.sleep(1)
