"""
app/services/rate_limit.py

In-memory rate limits, so nobody can drain the LLM credits, guess passwords
or flood the server.

| What                         | Limit                                        |
|------------------------------|----------------------------------------------|
| Chat questions (REST and WS) | 10 / minute and 200 / day per customer (or   |
|                              | per IP when signed out); 60 / minute overall |
| Sign-in attempts             | 20 / 10 minutes per IP                       |
| Failed sign-ins              | 10 / 15 minutes per email (any email, so it  |
|                              | reveals nothing about which are registered)  |
| Registrations                | 5 / hour per IP                              |
| Token refreshes              | 60 / minute per IP                           |
| Open WebSockets              | 5 at a time per IP                           |

The overall chat cap holds even against someone rotating IP addresses: it
bounds what the bot can spend per minute.

Counters live in this process. That is right for one app instance (the
Render setup); several instances would each count separately and need a
shared store such as Redis.
"""

import math
import threading
import time
from collections import deque

from app.services.errors import TooManyRequestsError

MAX_TRACKED_KEYS = 10_000  # beyond this, keys with no recent activity are dropped


class RateLimit:
    """At most `limit` events per `window` seconds per key (sliding window)."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._events: dict[str, deque] = {}

    def _recent(self, key: str, now: float) -> deque:
        events = self._events.setdefault(key, deque())
        while events and events[0] <= now - self.window:
            events.popleft()
        return events

    def retry_after(self, key: str) -> float:
        """Seconds until `key` may act again; 0 if it may now."""
        now = self.clock()
        events = self._recent(key, now)
        return 0.0 if len(events) < self.limit else events[0] + self.window - now

    def hit(self, key: str) -> None:
        now = self.clock()
        self._recent(key, now).append(now)
        if len(self._events) > MAX_TRACKED_KEYS:
            self._events = {k: v for k, v in self._events.items() if v and v[-1] > now - self.window}

    def reset(self) -> None:
        self._events.clear()


class ConnectionLimit:
    """At most `limit` simultaneous connections per key."""

    def __init__(self, limit: int):
        self.limit = limit
        self._open: dict[str, int] = {}

    def acquire(self, key: str) -> bool:
        with _lock:
            if self._open.get(key, 0) >= self.limit:
                return False
            self._open[key] = self._open.get(key, 0) + 1
            return True

    def release(self, key: str) -> None:
        with _lock:
            left = self._open.get(key, 0) - 1
            if left > 0:
                self._open[key] = left
            else:
                self._open.pop(key, None)

    def reset(self) -> None:
        self._open.clear()


_lock = threading.Lock()

CHAT_PER_MINUTE = RateLimit(10, 60)
CHAT_PER_DAY = RateLimit(200, 24 * 3600)
CHAT_OVERALL = RateLimit(60, 60)
LOGIN_PER_IP = RateLimit(20, 600)
LOGIN_FAILURES_PER_EMAIL = RateLimit(10, 900)
REGISTER_PER_IP = RateLimit(5, 3600)
REFRESH_PER_IP = RateLimit(60, 60)
WEBSOCKETS_PER_IP = ConnectionLimit(5)

ALL = [CHAT_PER_MINUTE, CHAT_PER_DAY, CHAT_OVERALL, LOGIN_PER_IP, LOGIN_FAILURES_PER_EMAIL,
       REGISTER_PER_IP, REFRESH_PER_IP, WEBSOCKETS_PER_IP]


def _wait_text(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = math.ceil(seconds / 60)
    return f"{minutes} minutes" if minutes < 90 else f"{math.ceil(minutes / 60)} hours"


def check(limits: list[tuple[RateLimit, str]], message: str) -> None:
    """Raise TooManyRequestsError if any limit is reached for its key."""
    with _lock:
        wait = max(limit.retry_after(key) for limit, key in limits)
    if wait > 0:
        seconds = max(1, math.ceil(wait))
        raise TooManyRequestsError(f"{message} Please try again in {_wait_text(seconds)}.", seconds)


def take(limits: list[tuple[RateLimit, str]], message: str) -> None:
    """Count one event against every limit, or -- if any is already reached
    -- count nothing and raise TooManyRequestsError."""
    with _lock:
        wait = max(limit.retry_after(key) for limit, key in limits)
        if wait <= 0:
            for limit, key in limits:
                limit.hit(key)
            return
    seconds = max(1, math.ceil(wait))
    raise TooManyRequestsError(f"{message} Please try again in {_wait_text(seconds)}.", seconds)


def take_chat(client_key: str) -> None:
    """Count one chat question (each may cost several LLM calls)."""
    take(
        [(CHAT_PER_MINUTE, client_key), (CHAT_PER_DAY, client_key), (CHAT_OVERALL, "all")],
        "You're sending messages faster than the assistant can answer.",
    )


def reset_all() -> None:
    for limit in ALL:
        limit.reset()
