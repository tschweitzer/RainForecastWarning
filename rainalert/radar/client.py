"""HTTP client for opendata.dwd.de.

Two jobs, and they pull in opposite directions:

* **Be a good citizen.** DWD publishes this for free. One request per cycle in the happy path,
  conditional requests, bounded retries, backoff, a circuit breaker and byte budgets (§4.3).
* **Do not trust the bytes.** A national weather service is not an adversary, but a compromised
  mirror, a hijacked route or a corrupt publication are indistinguishable here, and ``_LATEST``
  re-serves the same bytes every cycle - so one bad response is an indefinite outage unless it is
  bounded (§4.3.1).

Every limit that protects availability is therefore enforced *before* the body is in memory.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Self

import httpx

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """The cycle could not be fetched. Carries whether it is worth retrying."""


class ResponseTooLarge(FetchError):
    """The response exceeded the hard byte cap and was abandoned mid-stream."""


class BudgetExhausted(FetchError):
    """A byte budget is spent. Ingestion is halted - which means nobody gets warned."""


class BreakerOpen(FetchError):
    """Too many consecutive failures; not contacting DWD for now."""


@dataclass
class FetchResult:
    body: bytes | None  # None when the server answered 304
    etag: str | None
    last_modified: str | None
    not_modified: bool
    attempts: int


@dataclass
class _Budget:
    """Rolling byte budgets. Per-hour exists so exhaustion costs an hour, not a day."""

    hourly_limit: int
    daily_limit: int
    _events: list[tuple[datetime, int]] = field(default_factory=list)

    def spent(self, since: timedelta, now: datetime) -> int:
        cutoff = now - since
        return sum(n for ts, n in self._events if ts >= cutoff)

    def check(self, now: datetime) -> None:
        if self.spent(timedelta(hours=1), now) >= self.hourly_limit:
            raise BudgetExhausted("hourly byte budget exhausted")
        if self.spent(timedelta(days=1), now) >= self.daily_limit:
            raise BudgetExhausted("daily byte budget exhausted")

    def record(self, n: int, now: datetime) -> None:
        self._events.append((now, n))
        cutoff = now - timedelta(days=1)
        self._events = [(ts, v) for ts, v in self._events if ts >= cutoff]


class DWDClient:
    """Fetches the latest RV archive, politely and defensively."""

    def __init__(
        self,
        base_url: str,
        latest_name: str,
        user_agent: str,
        *,
        max_response_bytes: int,
        hourly_byte_budget: int,
        daily_byte_budget: int,
        max_attempts: int = 5,
        backoff_base_seconds: float = 20.0,
        timeout_seconds: float = 30.0,
        breaker_threshold: int = 5,
        breaker_cooldown_seconds: float = 900.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.url = base_url.rstrip("/") + "/" + latest_name
        self.max_response_bytes = max_response_bytes
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base_seconds
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = breaker_cooldown_seconds
        self._budget = _Budget(hourly_byte_budget, daily_byte_budget)
        self._sleep = sleep
        self._now = now
        self._consecutive_failures = 0
        self._breaker_until: datetime | None = None
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def breaker_open(self) -> bool:
        return self._breaker_until is not None and self._now() < self._breaker_until

    def fetch_latest(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        """One cycle's worth of fetching: at most ``max_attempts`` requests, then give up.

        Giving up is correct: the next cycle fires in five minutes and will try again. Retrying
        harder here only risks hammering DWD during an incident on their side.
        """
        if self.breaker_open:
            raise BreakerOpen(f"circuit breaker open until {self._breaker_until:%H:%M:%S}")
        self._budget.check(self._now())

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = self._attempt(headers, attempt)
            except ResponseTooLarge:
                self._record_failure()
                raise  # never retry: the same oversized bytes are still there
            except (httpx.HTTPError, FetchError) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    self._sleep(self._backoff(attempt))
                continue
            self._consecutive_failures = 0
            return result

        self._record_failure()
        raise FetchError(f"giving up after {self.max_attempts} attempts: {last_error}")

    def _attempt(self, headers: dict[str, str], attempt: int) -> FetchResult:
        with self._client.stream("GET", self.url, headers=headers) as response:
            if response.status_code == 304:
                response.close()
                return FetchResult(None, response.headers.get("etag"), None, True, attempt)
            if response.status_code in (429, 503):
                retry_after = response.headers.get("retry-after")
                response.close()
                if retry_after and retry_after.isdigit():
                    self._sleep(min(float(retry_after), 120.0))
                raise FetchError(f"server said {response.status_code}")
            if response.status_code != 200:
                response.close()
                raise FetchError(f"unexpected status {response.status_code}")

            # Content-Length is a hint from the other side, so it is checked *and* the stream is
            # counted. Trusting it alone would let a lying header through.
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_response_bytes:
                response.close()
                raise ResponseTooLarge(f"declared {declared} bytes")

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > self.max_response_bytes:
                    response.close()
                    raise ResponseTooLarge(f"stream exceeded {self.max_response_bytes} bytes")
                chunks.append(chunk)

        now = self._now()
        self._budget.record(total, now)
        return FetchResult(
            b"".join(chunks),
            response.headers.get("etag"),
            response.headers.get("last-modified"),
            False,
            attempt,
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential with jitter. Jitter matters: without it every deployment retries in lockstep."""
        base = self.backoff_base * (2 ** (attempt - 1))
        return base * (0.5 + random.random() * 0.5)

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.breaker_threshold:
            self._breaker_until = self._now() + timedelta(seconds=self.breaker_cooldown)
            logger.error(
                "circuit breaker opened after %d consecutive failures; not fetching until %s",
                self._consecutive_failures,
                self._breaker_until,
            )
