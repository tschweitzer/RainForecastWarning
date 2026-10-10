"""The service worker, executed rather than grepped.

`sw.js` is the only code that runs when the site is closed, and it is the last link in the chain
between a rain forecast and somebody looking at their phone. It had no behavioural coverage at all:
the tests that named it asserted that substrings appeared in the file, and they passed while

* `actions` was computed and never passed to `showNotification`, so no button was ever drawn and the
  entire action branch of `notificationclick` was unreachable - while `mail.py` had already dropped
  the unsubscribe URL from push bodies on the grounds that the button existed;
* `Notification.maxActions || 2` asked a platform reporting 0 (Safari) for two actions;
* the tab-reuse branch opened a new window for every warning after the first, so an afternoon of
  showers left a column of map tabs.

All three were found by running the worker. So the tests run it: `tests/js/sw_harness.mjs` builds a
fake worker global and `tests/js/sw_test.mjs` drives the listeners. This module is the bridge that
puts them in the suite, because a test only nobody runs is not a test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "tests" / "js" / "sw_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_service_worker_behaves(capsys):
    """Runs the node suite and fails with its output.

    Deliberately one test rather than one per case: the cases live in JavaScript because that is the
    language the code under test is written in, and mirroring their names here would be a list to
    keep in sync. The node runner prints `ok`/`FAIL` per case, and that output is what a failure
    shows.
    """
    result = subprocess.run(
        ["node", str(SUITE)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    with capsys.disabled():
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
    assert result.returncode == 0, f"service worker tests failed:\n{result.stdout}\n{result.stderr}"
    # Guards the bridge itself: if the runner ever stops finding cases it must not pass silently.
    assert "all 24 passed" in result.stdout, result.stdout


def test_the_worker_only_references_assets_that_exist():
    """`sw.js` hard-codes the icon and badge paths. Delete one and every notification silently falls
    back to Chrome's own default icon - no error, nothing in a log, and nothing in the suite."""
    import re

    source = (REPO / "rainalert" / "api" / "static" / "sw.js").read_text()
    referenced = set(re.findall(r"'(/static/[^']+)'", source))
    assert referenced, "expected the worker to reference at least the icon and the badge"
    for path in sorted(referenced):
        asset = REPO / "rainalert" / "api" / path.removeprefix("/")
        assert asset.is_file(), f"{path} is referenced by sw.js but not in the repo"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pages_own_javascript_behaves(tmp_path, capsys):
    """The same treatment for the signup page's own script.

    Fetches `/static/signup.js` and runs `tests/js/page_test.mjs` against it. Fetched through the
    app rather than read off disk for the same reason the page used to be rendered rather than read
    from the template: the test should cover what a browser is actually handed, so a route that
    stops serving the script fails here.
    """
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.config import Settings
    from rainalert.notify.webpush import generate_vapid_keys

    settings = Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.invalid",
        secret_key="test-secret",
        notifier="console",
        # A real keypair, and the *private* half, because that is the only one `Settings` has: the
        # public key is derived from it in `create_app`. This passed `vapid_public_key=...` for a
        # while, which is not a field - pydantic-settings dropped it silently, `vapid_private_key`
        # stayed empty, and the page rendered with `var VAPID_KEY = ""` i.e. push unavailable. The
        # harness had never once seen the configuration it exists to test, and the comment here
        # asserted the opposite. Generated rather than hard-coded so it cannot drift again.
        vapid_private_key=generate_vapid_keys()[0],
        vapid_subject="mailto:ops@rain.example.invalid",
        _env_file=None,
    )
    client = TestClient(create_app(settings))

    # Guards the setup itself, which is how the wrong-field bug survived: a page rendered with push
    # disabled still passes every assertion in page_test.mjs, because that file carries its own key.
    # The key used to be interpolated into the script as `var VAPID_KEY = ""`; since the extraction
    # it reaches the script through this attribute, so this is where an unconfigured key now shows.
    rendered = client.get("/").text
    assert 'data-vapid-key=""' not in rendered, (
        "the page was rendered with push disabled - the harness would be testing nothing"
    )

    script = tmp_path / "signup.js"
    response = client.get("/static/signup.js")
    assert response.status_code == 200, "the page's script is not being served"
    script.write_text(response.text)

    # Still the rendered page: manage.html carries its script inline.
    manage = tmp_path / "manage.html"
    manage.write_text(client.get("/manage").text)

    result = subprocess.run(
        ["node", str(REPO / "tests" / "js" / "page_test.mjs"), str(script), str(manage)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    with capsys.disabled():
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
    assert result.returncode == 0, (
        f"page javascript tests failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "all 29 passed" in result.stdout, result.stdout
