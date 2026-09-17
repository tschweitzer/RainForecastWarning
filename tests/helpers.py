"""Fixtures' worth of shared scaffolding that is not a pytest fixture.

Test modules import each other otherwise, which makes collecting a single file drag in an
unrelated one.
"""

from pathlib import Path

import httpx

from rainalert.radar.client import DWDClient

FIXTURES = Path(__file__).parent / "fixtures"

BODY = b"x" * 1024


class Recorder:
    """A transport that records requests and replays a scripted list of responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else httpx.Response(200, content=BODY)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_client(rec: Recorder, **kwargs):
    defaults = {
        "base_url": "https://opendata.dwd.de/weather/radar/composite/rv/",
        "latest_name": "DE1200_RV_LATEST.tar.bz2",
        "user_agent": "RainAlert/test (+https://example.invalid; contact: ops@example.invalid)",
        "max_response_bytes": 64 * 1024,
        "hourly_byte_budget": 1024 * 1024,
        "daily_byte_budget": 8 * 1024 * 1024,
        "transport": rec.transport(),
        "sleep": lambda _s: None,  # no real waiting in tests
    }
    defaults.update(kwargs)
    return DWDClient(**defaults)
