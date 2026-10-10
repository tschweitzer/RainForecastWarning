"""Scripts and styles by content-versioned URL (D-57).

The bug this exists for: `/static` had no Cache-Control, so browsers cached it heuristically, and
after a deploy Chrome, Edge and Firefox kept running the previous `radar.js` - the slider bubble
was live and only Opera, which had nothing cached, showed it.
"""

import re

import pytest
from fastapi.testclient import TestClient

from rainalert.api import assets
from rainalert.api.app import create_app
from rainalert.config import Settings
from rainalert.notify import ConsoleNotifier


@pytest.fixture()
def client(db, tmp_path):
    settings = Settings(
        database_url="postgresql+psycopg://unused",
        overlay_dir=str(tmp_path / "overlays"),
        secret_key="test-secret",
        _env_file=None,
    )
    return TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))


@pytest.mark.parametrize("path", ["/", "/confirm", "/privacy"])
def test_every_page_asks_for_the_current_version(client, path):
    """A bare `/static/x.js` in a page is the bug again: cached on a guess, kept after a deploy."""
    page = client.get(path).text
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    assert refs, f"{path} references no static files - did the pattern stop matching?"
    for ref in refs:
        name, _, query = ref.removeprefix("/static/").partition("?")
        assert query == f"v={assets.version_of(assets.STATIC_DIR / name)}", ref


def test_the_current_version_is_kept_for_a_year(client):
    url = assets.static_url("radar.js")
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "url",
    [
        "/static/radar.js",  # unversioned: what sw.js asks for its icons
        "/static/radar.js?v=000000000000",  # a page from before a deploy
    ],
)
def test_anything_else_is_checked_before_reuse(client, url):
    """A stale `v` gets the *new* file, so it must not be pinned to the old URL for a year."""
    assert client.get(url).headers["cache-control"] == "no-cache"


def test_a_revalidation_still_answers_304(client):
    etag = client.get("/static/radar.js").headers["etag"]
    response = client.get("/static/radar.js", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.headers["cache-control"] == "no-cache"


def test_the_version_follows_the_content(tmp_path):
    """Recomputed when the file changes, not cached from startup: locally the files are edited
    under a running server, and a stale version there would come with a one-year header."""
    path = tmp_path / "x.js"
    path.write_text("one")
    first = assets.version_of(path)
    path.write_text("two, and longer")
    assert assets.version_of(path) != first
    path.write_text("one")
    assert assets.version_of(path) == first
