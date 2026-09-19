"""Politeness and defensive limits of the DWD client (DESIGN.md §4.3, §4.3.1).

These rules are invisible when they work: nothing breaks if we quietly send ten requests a cycle,
right up until DWD blocks us. So they are pinned here rather than trusted to review.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rainalert.radar.client import (
    BreakerOpen,
    BudgetExhausted,
    FetchError,
    ResponseTooLarge,
)
from tests.helpers import BODY, Recorder, make_client


def test_happy_path_is_exactly_one_request():
    """The single most important politeness property."""
    rec = Recorder(httpx.Response(200, content=BODY, headers={"ETag": '"abc"'}))
    with make_client(rec) as client:
        result = client.fetch_latest()
    assert len(rec.requests) == 1
    assert result.attempts == 1
    assert result.body == BODY
    assert result.etag == '"abc"'


def test_identifies_itself_with_a_contact_address():
    rec = Recorder()
    with make_client(rec) as client:
        client.fetch_latest()
    agent = rec.requests[0].headers["user-agent"]
    assert agent.startswith("RainAlert/")
    assert "contact:" in agent


def test_sends_conditional_headers_when_we_have_seen_a_cycle():
    rec = Recorder(httpx.Response(304))
    with make_client(rec) as client:
        result = client.fetch_latest(etag='"abc"', last_modified="Wed, 16 Sep 2026 09:23:30 GMT")
    assert rec.requests[0].headers["if-none-match"] == '"abc"'
    assert rec.requests[0].headers["if-modified-since"] == "Wed, 16 Sep 2026 09:23:30 GMT"
    assert result.not_modified and result.body is None


def test_retries_are_bounded_and_then_it_gives_up():
    """Five attempts, then stop. The next cycle is only five minutes away."""
    rec = Recorder(*[httpx.Response(500) for _ in range(10)])
    with make_client(rec, max_attempts=5) as client, pytest.raises(FetchError, match="giving up"):
        client.fetch_latest()
    assert len(rec.requests) == 5


def test_backs_off_with_jitter_between_attempts():
    slept: list[float] = []
    rec = Recorder(*[httpx.Response(500) for _ in range(3)], httpx.Response(200, content=BODY))
    with make_client(rec, max_attempts=4, sleep=slept.append, backoff_base_seconds=20.0) as client:
        client.fetch_latest()
    assert len(slept) == 3
    assert slept == sorted(slept)  # increasing
    assert all(10.0 <= s <= 80.0 for s in slept)  # base*2^n with 0.5-1.0 jitter
    assert len(set(slept)) == 3  # jittered, not lockstep


def test_honours_retry_after_on_429():
    slept: list[float] = []
    rec = Recorder(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, content=BODY),
    )
    with make_client(rec, sleep=slept.append) as client:
        client.fetch_latest()
    # Exactly one wait, of the length the server asked for. It used to sleep the Retry-After and
    # then the exponential backoff on top, so a rate-limited request waited for both.
    assert slept == [7.0]


def test_oversized_declared_response_is_refused_without_reading_it():
    rec = Recorder(httpx.Response(200, content=BODY, headers={"Content-Length": str(1 << 30)}))
    with make_client(rec) as client, pytest.raises(ResponseTooLarge, match="declared"):
        client.fetch_latest()


def test_absent_content_length_is_caught_by_counting_the_stream():
    """A chunked response declares no length at all, so the bytes themselves must be counted."""

    def chunks():
        for _ in range(16):
            yield b"y" * (16 * 1024)

    rec = Recorder(httpx.Response(200, content=chunks()))
    with (
        make_client(rec, max_response_bytes=64 * 1024) as client,
        pytest.raises(ResponseTooLarge, match="stream exceeded"),
    ):
        client.fetch_latest()
    assert "content-length" not in rec.requests[0].headers


def test_oversized_response_is_not_retried():
    """The same oversized bytes are still there; retrying only wastes DWD's bandwidth."""
    rec = Recorder(*[httpx.Response(200, content=b"y" * (128 * 1024)) for _ in range(5)])
    with (
        make_client(rec, max_response_bytes=64 * 1024) as client,
        pytest.raises(ResponseTooLarge),
    ):
        client.fetch_latest()
    assert len(rec.requests) == 1


def test_byte_budget_halts_fetching():
    rec = Recorder(*[httpx.Response(200, content=BODY) for _ in range(20)])
    with make_client(rec, hourly_byte_budget=2048) as client:
        client.fetch_latest()
        client.fetch_latest()
        with pytest.raises(BudgetExhausted, match="hourly"):
            client.fetch_latest()


def test_circuit_breaker_opens_after_consecutive_failures():
    rec = Recorder(*[httpx.Response(500) for _ in range(100)])
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    with make_client(rec, max_attempts=1, breaker_threshold=3, now=lambda: now) as client:
        for _ in range(3):
            with pytest.raises(FetchError):
                client.fetch_latest()
        assert client.breaker_open
        with pytest.raises(BreakerOpen):
            client.fetch_latest()
    assert len(rec.requests) == 3  # no request made once the breaker is open


def test_breaker_closes_after_the_cooldown():
    rec = Recorder(*[httpx.Response(500) for _ in range(3)], httpx.Response(200, content=BODY))
    clock = [datetime(2026, 9, 16, 12, 0, tzinfo=UTC)]
    with make_client(
        rec, max_attempts=1, breaker_threshold=3, breaker_cooldown_seconds=900, now=lambda: clock[0]
    ) as client:
        for _ in range(3):
            with pytest.raises(FetchError):
                client.fetch_latest()
        assert client.breaker_open
        clock[0] += timedelta(seconds=901)
        assert not client.breaker_open
        assert client.fetch_latest().body == BODY


def test_a_successful_fetch_resets_the_failure_count():
    rec = Recorder(
        httpx.Response(500),
        httpx.Response(200, content=BODY),
        httpx.Response(500),
        httpx.Response(500),
    )
    with make_client(rec, max_attempts=1, breaker_threshold=3) as client:
        with pytest.raises(FetchError):
            client.fetch_latest()
        client.fetch_latest()
        for _ in range(2):
            with pytest.raises(FetchError):
                client.fetch_latest()
        assert not client.breaker_open


def test_redirects_are_not_followed():
    """A redirect off opendata.dwd.de is an integrity signal, not a convenience.

    Following one would let anyone who can answer for that host move us to a body of their choosing
    while every other control still reads as normal.
    """
    rec = Recorder(
        *[
            httpx.Response(302, headers={"Location": "https://elsewhere.invalid/x"})
            for _ in range(5)
        ]
    )
    with make_client(rec, max_attempts=5) as client, pytest.raises(FetchError, match="302"):
        client.fetch_latest()
    assert all(str(r.url).startswith("https://opendata.dwd.de/") for r in rec.requests)
