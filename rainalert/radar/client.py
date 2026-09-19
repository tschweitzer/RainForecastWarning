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
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Self

import httpx

logger = logging.getLogger(__name__)


#: `DE1200_RV2609181435.tar.bz2` and nothing else - no separators, no escapes, no traversal.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_]+\.tar\.bz2")


def archive_name(nominal_time: datetime) -> str:
    """The timestamped file name DWD publishes for a cycle (DWD_RV_FORMAT.md 1).

    Note the two-digit year: the directory listing uses `YYMMDDHHMM`, not `YYYY`.
    """
    return f"DE1200_RV{nominal_time.astimezone(UTC):%y%m%d%H%M}.tar.bz2"


class FetchError(RuntimeError):
    """The cycle could not be fetched. Carries whether it is worth retrying."""


class ResponseTooLarge(FetchError):
    """The response exceeded the hard byte cap and was abandoned mid-stream."""


class BudgetExhausted(FetchError):
    """A byte budget is spent. Ingestion is halted - which means nobody gets warned."""


class ArchiveNotFound(FetchError):
    """404. The cycle is past DWD's retention window, or never existed.

    Separate from FetchError because it must never be retried: five attempts with exponential
    backoff against a file that is genuinely gone is five pointless requests and several minutes
    of waiting, which is the opposite of polite.
    """


class ServerBusy(FetchError):
    """429 or 503. Carries the wait the server asked for, when it named one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + "/" + latest_name
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
            headers={
                "User-Agent": user_agent,
                # A .tar.bz2 is already compressed. httpx asks for `gzip, deflate` by default,
                # which invites the server to spend CPU re-compressing incompressible bytes into
                # a payload the same size or larger - 576 times over in a backfill, on a service
                # DWD provides for free (§4.3: do not make them work for nothing). `identity`
                # says plainly that we want the file as it is on disk.
                "Accept-Encoding": "identity",
            },
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
        return self._fetch(self.url, etag, last_modified)

    def fetch_named(self, name: str) -> FetchResult:
        """Fetch one timestamped archive by file name, for backfilling past cycles.

        The name is built from a datetime by ``archive_name`` and never from anything a user
        typed; it is checked here anyway, because a path separator or an escape in a URL this
        code builds is the kind of thing that is obvious only in hindsight.
        """
        if not _SAFE_NAME.fullmatch(name):
            raise ValueError(f"refusing to fetch {name!r}: not a plain archive name")
        return self._fetch(f"{self.base_url}/{name}")

    def _fetch(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
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
                result = self._attempt(url, headers, attempt)
            except ResponseTooLarge:
                self._record_failure()
                raise  # never retry: the same oversized bytes are still there
            except ArchiveNotFound:
                raise  # the file is not there; asking again four more times will not change that
            except (httpx.HTTPError, FetchError) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    # A server that named a wait knows better than our exponential guess.
                    asked = getattr(exc, "retry_after", None)
                    self._sleep(asked if asked is not None else self._backoff(attempt))
                continue
            self._consecutive_failures = 0
            return result

        self._record_failure()
        raise FetchError(f"giving up after {self.max_attempts} attempts: {last_error}")

    def _attempt(self, url: str, headers: dict[str, str], attempt: int) -> FetchResult:
        with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304:
                response.close()
                return FetchResult(None, response.headers.get("etag"), None, True, attempt)
            if response.status_code in (429, 503):
                header = response.headers.get("retry-after")
                response.close()
                # Carried, not slept on here. Sleeping now *and* letting the retry loop back off
                # meant a rate-limited request waited for both - up to 120 s plus the exponential
                # backoff, on every attempt. The caller waits once, for whichever is right.
                wait = min(float(header), 120.0) if header and header.isdigit() else None
                raise ServerBusy(f"server said {response.status_code}", wait)
            if response.status_code == 404:
                response.close()
                raise ArchiveNotFound(url.rsplit("/", 1)[-1])
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
