"""
Tests for rate limits (app/services/rate_limit.py), the client IP they key
on (app/api/deps.client_ip) and the deployment helpers. No database needed.
"""

from types import SimpleNamespace as NS

import pytest

from app.api.deps import chat_client_key, client_ip
from app.config import docs_enabled
from app.db.session import sqlalchemy_url
from app.services import rate_limit
from app.services.errors import TooManyRequestsError
from app.services.rate_limit import ConnectionLimit, RateLimit


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_sliding_window_allows_the_limit_then_waits_for_the_oldest_to_expire():
    clock = Clock()
    limit = RateLimit(3, 60, clock)
    for _ in range(3):
        assert limit.retry_after("a") == 0
        limit.hit("a")
        clock.now += 10
    assert limit.retry_after("a") == pytest.approx(30)  # the first hit was 30 s ago
    assert limit.retry_after("b") == 0  # keys are independent
    clock.now += 30
    assert limit.retry_after("a") == 0


def test_take_counts_nothing_when_any_limit_is_reached():
    clock = Clock()
    small, big = RateLimit(1, 60, clock), RateLimit(100, 60, clock)
    rate_limit.take([(small, "k"), (big, "k")], "Slow down.")
    with pytest.raises(TooManyRequestsError) as error:
        rate_limit.take([(small, "k"), (big, "k")], "Slow down.")
    assert error.value.status_code == 429 and error.value.headers == {"Retry-After": "60"}
    assert "Slow down. Please try again in 60 seconds." == error.value.detail
    assert len(big._events["k"]) == 1  # the refused attempt wasn't counted


def test_check_never_counts():
    limit = RateLimit(1, 60, Clock())
    for _ in range(5):
        rate_limit.check([(limit, "k")], "x")
    assert limit.retry_after("k") == 0


def test_chat_is_limited_per_client_and_overall():
    for _ in range(10):
        rate_limit.take_chat("customer:1")
    with pytest.raises(TooManyRequestsError):
        rate_limit.take_chat("customer:1")
    for _ in range(10):
        rate_limit.take_chat("customer:2")  # someone else is unaffected
    for n in range(40):
        rate_limit.take_chat(f"ip:10.0.0.{n}")  # 60 overall now used
    with pytest.raises(TooManyRequestsError):
        rate_limit.take_chat("ip:10.0.1.1")  # a brand-new IP still hits the overall cap


def test_connection_limit():
    limit = ConnectionLimit(2)
    assert limit.acquire("ip") and limit.acquire("ip") and not limit.acquire("ip")
    assert limit.acquire("other")
    limit.release("ip")
    assert limit.acquire("ip")


def test_old_keys_are_dropped_when_too_many_are_tracked(monkeypatch):
    clock = Clock()
    limit = RateLimit(5, 60, clock)
    monkeypatch.setattr(rate_limit, "MAX_TRACKED_KEYS", 3)
    for k in "abc":
        limit.hit(k)
    clock.now += 61
    limit.hit("d")
    assert set(limit._events) == {"d"}


def conn(xff=None, host="10.0.0.9"):
    headers = {"x-forwarded-for": xff} if xff else {}
    return NS(headers=headers, client=NS(host=host))


def test_client_ip_ignores_forwarded_headers_unless_behind_a_trusted_proxy(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    assert client_ip(conn("6.6.6.6")) == "10.0.0.9"


def test_behind_the_proxy_only_the_entry_it_appended_counts(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "true")
    # A client can put anything in front; the proxy appends the real address last.
    assert client_ip(conn("6.6.6.6, 7.7.7.7, 203.0.113.5")) == "203.0.113.5"
    assert client_ip(conn("203.0.113.5")) == "203.0.113.5"
    assert client_ip(conn(None)) == "10.0.0.9"


def test_chat_allowance_is_per_customer_when_signed_in():
    assert chat_client_key({"id": 7}, "1.2.3.4") == "customer:7"
    assert chat_client_key(None, "1.2.3.4") == "ip:1.2.3.4"


@pytest.mark.parametrize("url", ["postgres://u:p@h:5432/d", "postgresql://u:p@h:5432/d"])
def test_both_postgres_url_spellings_use_psycopg2(url):
    assert sqlalchemy_url(url).drivername == "postgresql+psycopg2"


def test_docs_can_be_switched_off(monkeypatch):
    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    assert docs_enabled()
    monkeypatch.setenv("ENABLE_DOCS", "false")
    assert not docs_enabled()
