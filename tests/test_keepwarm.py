"""The keep-warm request at the end of each ingest run (D-53).

It is bolted onto the job that warns people, so what matters most is what it must not do: raise,
change the exit code, or run before the work that matters.
"""

import ast
import inspect
import logging

import httpx
import pytest

from rainalert.jobs import keepwarm
from rainalert.jobs.keepwarm import keep_warm

URL = "https://rainalert-api-example.a.run.app/healthz"


def test_an_empty_url_sends_nothing():
    def handler(request):
        raise AssertionError("no request may be made when keep-warm is off")

    assert keep_warm("", transport=httpx.MockTransport(handler)) is None


def test_it_requests_the_url_once_and_reports_the_time():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    elapsed = keep_warm(URL, transport=httpx.MockTransport(handler))
    assert elapsed is not None and elapsed >= 0
    assert len(seen) == 1
    assert seen[0].method == "GET" and str(seen[0].url) == URL
    assert seen[0].headers["user-agent"] == "RainAlert keep-warm"


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), RuntimeError("anything at all")],
)
def test_no_failure_escapes(failure, caplog):
    def handler(request):
        raise failure

    with caplog.at_level(logging.WARNING, logger="rainalert.jobs.keepwarm"):
        assert keep_warm(URL, transport=httpx.MockTransport(handler)) is None
    assert "keep-warm request failed" in caplog.text
    # The exception's type, not its message: a message can carry a URL with a token in it.
    assert type(failure).__name__ in caplog.text


def test_an_error_status_is_logged_not_raised(caplog):
    with caplog.at_level(logging.WARNING, logger="rainalert.jobs.keepwarm"):
        result = keep_warm(URL, transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert result is not None
    assert "answered 503" in caplog.text


def test_redirects_are_not_followed():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/"})

    keep_warm(URL, transport=httpx.MockTransport(handler))
    assert hosts == ["rainalert-api-example.a.run.app"]


def test_a_cold_service_is_logged_so_it_can_be_counted(monkeypatch, caplog):
    """The log line is how to tell whether keeping warm works: often means it does not."""
    clock = iter([100.0, 107.9])
    monkeypatch.setattr(keepwarm.time, "monotonic", lambda: next(clock))
    with caplog.at_level(logging.INFO, logger="rainalert.jobs.keepwarm"):
        keep_warm(URL, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert "found the web service cold: 7.9 s" in caplog.text


def test_the_ingest_command_keeps_warm_last_and_unconditionally():
    """After the cycle and pruning, outside every block, and never feeding the exit code.

    Checked on the source because the alternative is a full ingest run against a database and a
    fake DWD to observe one ordering - and the ordering is what this is about: a request that runs
    before the cycle delays warnings, and one inside a `try` or `if` can be skipped or swallow
    something it should not.
    """
    from rainalert import cli

    tree = ast.parse(inspect.getsource(cli.ingest))
    body = tree.body[0].body
    calls = [
        index
        for index, stmt in enumerate(body)
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and getattr(stmt.value.func, "id", None) == "keep_warm"
    ]
    assert len(calls) == 1, "keep_warm must be one top-level statement of ingest()"
    work = max(i for i, stmt in enumerate(body) if isinstance(stmt, ast.With))
    assert calls[0] > work, "keep_warm must run after the cycle, not before it"
    assert ast.unparse(body[calls[0]]) == "keep_warm(settings.keep_warm_url)", (
        "its result must not be used - in particular not for the exit code"
    )
