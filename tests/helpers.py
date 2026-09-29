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


def page_source(client, path: str = "/") -> str:
    """The page *and* the same-origin scripts it loads, concatenated.

    Assertions like `assert "function sameKey(" in body` are about what reaches the browser, not
    about which file it arrived in. They were written when the signup page carried its JavaScript
    inline; moving that to `/static/signup.js` would have broken fourteen of them without changing
    a single thing a reader experiences.

    So this returns the union, which is what those assertions always meant. It follows only
    same-origin `src` attributes - an external script is somebody else's source and this file has
    opinions about that (there are none left: `script-src` is `'self'`).

    **Use it for "does this code ship", not for "does the reader see this text."** The union
    contains radar.js, which says "mm/h" inside a tooltip formatter; asserting that a page never
    shows "mm/h" against the union is a true statement about the source and a false one about the
    page. For copy, read `client.get(path).text` directly.
    """
    import re

    html = client.get(path).text
    sources = [html]
    for src in re.findall(r'<script[^>]+src="(/[^"]+)"', html):
        sources.append(client.get(src).text)
    return "\n".join(sources)


def js_function(source: str, name: str) -> str:
    """The body of a JavaScript function, found by matching braces rather than by a landmark.

    Written because a landmark broke. Two tests sliced `start()` out of manage.html as everything
    between ``async function start()`` and the literal ``start();`` that called it - and when that
    call became ``restart();``, the boundary matched the ``start();`` *inside* it. The slice then ran
    on into the next function, whose concurrency guard has a bare `return`, and a test about the
    settings dispatch failed for a reason that had nothing to do with the dispatch.

    Brace matching has no such ambiguity: a function ends where its own braces close.
    """
    at = source.index("function " + name + "(")
    depth = 0
    started = False
    for i in range(at, len(source)):
        if source[i] == "{":
            depth += 1
            started = True
        elif source[i] == "}":
            depth -= 1
            if started and depth == 0:
                return source[at : i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")
