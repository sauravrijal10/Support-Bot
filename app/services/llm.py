"""
app/services/llm.py

Thin wrapper around the Hugging Face chat client. It owns the model name,
provider and error handling, so the rest of the code just calls
`llm.complete(messages, tools=...)`.

The free/cheap tiers are flaky, so it retries once on a transient
server/network error (not on 429 -- retrying a rate limit only makes it
worse) and turns every provider failure into a clean 503 instead of an
unhandled 500.
"""

import time

import httpx
from huggingface_hub import InferenceClient
from huggingface_hub.errors import HfHubHTTPError

from app.services.errors import ServiceUnavailableError

MAX_OUTPUT_TOKENS = 500
# Low temperature: tool arguments and "answer only from the data" both want
# consistent behaviour more than creativity.
TEMPERATURE = 0.2


class LLMClient:
    def __init__(self, api_key: str, model: str, provider: str = "auto"):
        self.model = model
        self._client = InferenceClient(api_key=api_key, provider=provider)

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

        for attempt in (1, 2):
            try:
                return self._client.chat.completions.create(**kwargs)
            except HfHubHTTPError as error:
                code = getattr(error.response, "status_code", None)
                if code == 429:
                    raise ServiceUnavailableError(
                        "The assistant is receiving too many requests right now. "
                        "Please try again in a moment."
                    ) from error
                transient = code is None or code >= 500
            except httpx.TransportError:
                transient = True
            if not transient or attempt == 2:
                raise ServiceUnavailableError(
                    "The assistant is temporarily unavailable. Please try again shortly."
                )
            time.sleep(1)
