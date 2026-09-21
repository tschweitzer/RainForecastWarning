"""The pages' own assets and wiring.

Browser behaviour cannot be asserted here - geolocation needs a browser, and that was checked
against Chromium separately. What these tests hold in place is the wiring that made the old
button silently do nothing: the helper being served at all, every page actually loading it, and
each page having somewhere to put a message when the attempt fails.
"""

import pytest
from fastapi.testclient import TestClient

from rainalert.api.app import create_app
from rainalert.config import Settings
from rainalert.notify import ConsoleNotifier


@pytest.fixture()
def settings():
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.invalid",
        secret_key="test-secret",
        notifier="console",
        _env_file=None,
    )


@pytest.fixture()
def client(db, settings):
    app = create_app(settings, session_factory=db, notifier=ConsoleNotifier())
    return TestClient(app, base_url=settings.public_base_url)


def test_the_geolocation_helper_is_served(client):
    response = client.get("/static/geolocate.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/", "/map", "/manage"])
def test_every_page_with_a_locate_button_loads_the_helper(client, path):
    assert "/static/geolocate.js" in client.get(path).text


@pytest.mark.parametrize("path", ["/", "/manage"])
def test_the_form_pages_have_somewhere_to_show_a_failure(client, path):
    # Without this element the handler has nowhere to report, which is how the button came to
    # fail silently in the first place.
    assert 'id="locate-status"' in client.get(path).text


def test_the_map_page_has_an_on_map_control(client):
    page = client.get("/map").text
    assert "locate-control" in page
    assert "Zu meinem Standort" in page


@pytest.mark.parametrize(
    "fragment",
    [
        "isSecureContext",  # the http-on-a-VM case, which no page code can fix
        "timeout",  # or a browser that never answers leaves the button busy forever
        "error.code === 1",  # denied
        "error.code === 2",  # position unavailable
        "error.code === 3",  # timed out
    ],
)
def test_the_helper_handles_every_way_geolocation_declines(client, fragment):
    """Each of these is a path that used to end in silence."""
    assert fragment in client.get("/static/geolocate.js").text


def test_the_csp_allows_the_helper_but_not_arbitrary_script(client):
    policy = client.get("/").headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "'unsafe-inline'" not in policy.split("style-src")[0]


# --- readiness ---------------------------------------------------------------------------------


def test_readyz_is_ready_on_a_current_schema(client):
    assert client.get("/readyz").status_code == 200


def test_readyz_refuses_a_schema_that_is_behind(client, db):
    """The failure this exists for: new code deployed over an un-migrated database.

    It connects fine and then 500s on the first request touching what the migration added, a
    long way from the cause. Readiness is where that belongs.
    """
    from sqlalchemy import text

    with db() as session:
        session.execute(text("UPDATE alembic_version SET version_num = 'deadbeef1234'"))
        session.commit()

    response = client.get("/readyz")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "deadbeef1234" in detail  # what it is
    assert "make migrate" in detail  # what to do about it


def test_healthz_stays_up_when_the_schema_is_behind(client, db):
    """Liveness is not readiness: the process is fine, it just must not take traffic."""
    from sqlalchemy import text

    with db() as session:
        session.execute(text("UPDATE alembic_version SET version_num = 'deadbeef1234'"))
        session.commit()
    assert client.get("/healthz").status_code == 200


def test_a_database_with_no_alembic_version_is_reported(client, db):
    from sqlalchemy import text

    with db() as session:
        session.execute(text("DROP TABLE alembic_version"))
        session.commit()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "no schema yet" in response.json()["detail"]
