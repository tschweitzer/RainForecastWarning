"""The self-service settings page: how you get in, and what you may change once you are.

Weighted towards the things that are invisible when they break. A magic link that stays valid
after use, a write that a third-party page can trigger, an address the endpoint confirms exists,
a threshold that becomes a 500 in the database rather than a sentence on the form - none of those
show up by clicking through the page once.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rainalert.api.app import CSRF_HEADER, MANAGE_COOKIE, create_app
from rainalert.config import Settings
from rainalert.db.models import AuthToken, Subscriber, Subscription, TokenPurpose
from rainalert.notify import ConsoleNotifier
from rainalert.tokens import csrf_token, session_token, verify_csrf_token

MUNICH = (48.1533, 11.5574)
HAMBURG = (53.5511, 9.9937)


@pytest.fixture()
def settings():
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.invalid",
        mail_from="RainAlert <noreply@rain.example.invalid>",
        secret_key="test-secret",
        notifier="console",
        _env_file=None,
    )


@pytest.fixture()
def notifier():
    return ConsoleNotifier()


@pytest.fixture()
def client(db, settings, notifier):
    # Same scheme as `public_base_url`, because the session cookie is Secure when that is https -
    # and a Secure cookie sent over http is silently dropped by the client, which would make
    # every test here fail as "not authorised" for a reason that has nothing to do with auth.
    return TestClient(
        create_app(settings, session_factory=db, notifier=notifier),
        base_url=settings.public_base_url,
    )


def subscribed(client, notifier, email="friend@example.com", lat=MUNICH[0], lon=MUNICH[1]):
    """A confirmed, active email subscriber."""
    client.post("/api/v1/subscriptions", json={"email": email, "lat": lat, "lon": lon})
    token = notifier.sent[-1].text.split("token=")[1].split()[0]
    client.post("/confirm", data={"token": token})
    return email


def link_token(notifier) -> str:
    """The magic link puts its token in the fragment, not the query string."""
    body = notifier.sent[-1].text
    assert "/manage#t=" in body, "the token must ride in the fragment (F-4/F-8)"
    return body.split("/manage#t=")[1].split()[0]


def signed_in(client, notifier, **kwargs) -> str:
    subscribed(client, notifier, **kwargs)
    client.post("/api/v1/manage/link", json={"channel": "email", "address": "friend@example.com"})
    response = client.post("/api/v1/manage/session", data={"token": link_token(notifier)})
    assert response.status_code == 200
    return response.json()["csrf"]


def put_session_cookie(client, value):
    """Replace the session cookie.

    Set without a matching domain and path, httpx keeps the server's copy alongside the new
    one and then refuses to say which is current - CookieConflict, from the test rather than
    from anything the code did.
    """
    client.cookies.delete(MANAGE_COOKIE)
    client.cookies.set(MANAGE_COOKIE, value, domain="rain.example.invalid", path="/")


def write(client, csrf, method="PATCH", url="/api/v1/subscriptions/me", **body):
    return client.request(method, url, json=body, headers={CSRF_HEADER: csrf})


# --- getting in -------------------------------------------------------------------------------


def test_the_link_opens_a_session_that_can_read_and_write(client, notifier, db):
    csrf = signed_in(client, notifier)
    me = client.get("/api/v1/subscriptions/me")
    assert me.status_code == 200
    assert me.json()["address"] == "friend@example.com"
    assert write(client, csrf, threshold_mm_5min=0.25).status_code == 204
    with db() as session:
        assert float(session.query(Subscription).one().threshold_mm_5min) == 0.25


def test_the_link_works_exactly_once(client, notifier):
    subscribed(client, notifier)
    client.post("/api/v1/manage/link", json={"channel": "email", "address": "friend@example.com"})
    token = link_token(notifier)
    assert client.post("/api/v1/manage/session", data={"token": token}).status_code == 200
    # A link to someone's home coordinates sitting in an inbox must stop working once spent.
    assert client.post("/api/v1/manage/session", data={"token": token}).status_code == 401


def test_a_second_request_supersedes_the_first_link(client, notifier, db):
    subscribed(client, notifier)
    body = {"channel": "email", "address": "friend@example.com"}
    client.post("/api/v1/manage/link", json=body)
    first = link_token(notifier)
    client.post("/api/v1/manage/link", json=body)
    assert client.post("/api/v1/manage/session", data={"token": first}).status_code == 401
    with db() as session:
        live = session.execute(
            select(AuthToken).where(AuthToken.purpose == TokenPurpose.MANAGE)
        ).scalars()
        assert len([t for t in live if t.used_at is None]) == 1


def test_an_expired_link_is_refused(client, notifier, db):
    subscribed(client, notifier)
    client.post("/api/v1/manage/link", json={"channel": "email", "address": "friend@example.com"})
    token = link_token(notifier)
    with db() as session:
        row = session.execute(
            select(AuthToken).where(AuthToken.purpose == TokenPurpose.MANAGE)
        ).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
    assert client.post("/api/v1/manage/session", data={"token": token}).status_code == 401


def test_an_unknown_address_answers_the_same_and_sends_nothing(client, notifier):
    subscribed(client, notifier)
    before = len(notifier.sent)
    response = client.post(
        "/api/v1/manage/link", json={"channel": "email", "address": "stranger@example.com"}
    )
    # Same status and same body as the hit, or the page is a subscriber-list oracle.
    assert response.status_code == 202
    assert len(notifier.sent) == before


def test_an_unconfirmed_subscriber_gets_no_link(client, notifier):
    client.post("/api/v1/subscriptions", json={"email": "new@example.com", "lat": 50.1, "lon": 8.6})
    before = len(notifier.sent)
    client.post("/api/v1/manage/link", json={"channel": "email", "address": "new@example.com"})
    # Confirmation is what proves the channel reaches the person; a settings link does not get
    # to take that on trust.
    assert len(notifier.sent) == before


def test_requesting_links_is_rate_limited(client, notifier, settings):
    subscribed(client, notifier)
    body = {"channel": "email", "address": "friend@example.com"}
    for _ in range(settings.manage_link_limit_per_hour):
        assert client.post("/api/v1/manage/link", json=body).status_code == 202
    assert client.post("/api/v1/manage/link", json=body).status_code == 429


# --- CSRF, and why the cookie is not enough ----------------------------------------------------


def test_a_write_without_the_form_token_is_refused(client, notifier):
    signed_in(client, notifier)
    # The cookie rides along on any request the browser makes, including one another site
    # caused. The form token cannot, because reading it needs same-origin access (F-16).
    assert (
        client.patch("/api/v1/subscriptions/me", json={"threshold_mm_5min": 9.0}).status_code == 403
    )


def test_reads_do_not_need_the_form_token(client, notifier):
    signed_in(client, notifier)
    assert client.get("/api/v1/subscriptions/me").status_code == 200


def test_another_subscribers_form_token_does_not_work(client, notifier, db, settings):
    csrf = signed_in(client, notifier)
    # A genuinely valid token belonging to somebody else - not an expired one, which would
    # be refused for the wrong reason and prove nothing about whose session it belongs to.
    future = int((datetime.now(UTC) + timedelta(minutes=30)).timestamp())
    stranger = csrf_token(uuid.uuid4(), settings.secret_key, future)
    assert verify_csrf_token(stranger, settings.secret_key) is not None, "must be valid to test"
    assert write(client, stranger, threshold_mm_5min=9.0).status_code == 403
    assert write(client, csrf, threshold_mm_5min=9.0).status_code == 204


def test_an_expired_session_is_refused(client, notifier, db, settings):
    signed_in(client, notifier)
    with db() as session:
        subscriber = session.query(Subscriber).one()
        stale = session_token(subscriber.id, settings.secret_key, -1)
    put_session_cookie(client, stale)
    assert client.get("/api/v1/subscriptions/me").status_code == 401


def test_a_session_for_a_deleted_subscriber_is_refused(client, notifier, db):
    signed_in(client, notifier)
    with db() as session:
        session.delete(session.query(Subscriber).one())
        session.commit()
    assert client.get("/api/v1/subscriptions/me").status_code == 401


def test_the_session_cookie_is_httponly_and_secure_on_https(client, notifier):
    """Script must not be able to read it, and it must not travel in clear."""
    subscribed(client, notifier)
    client.post("/api/v1/manage/link", json={"channel": "email", "address": "friend@example.com"})
    response = client.post("/api/v1/manage/session", data={"token": link_token(notifier)})
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "secure" in header


def test_the_cookie_is_not_secure_when_the_site_is_served_over_http(db, notifier, settings):
    """Otherwise the browser drops it and local development looks like a broken login."""
    plain = settings.model_copy(update={"public_base_url": "http://localhost:8000"})
    local = TestClient(
        create_app(plain, session_factory=db, notifier=notifier), base_url=plain.public_base_url
    )
    subscribed(local, notifier)
    local.post("/api/v1/manage/link", json={"channel": "email", "address": "friend@example.com"})
    response = local.post("/api/v1/manage/session", data={"token": link_token(notifier)})
    assert "secure" not in response.headers["set-cookie"].lower()
    assert local.get("/api/v1/subscriptions/me").status_code == 200


def test_logging_out_ends_the_session(client, notifier):
    signed_in(client, notifier)
    assert client.post("/api/v1/manage/logout").status_code == 204
    assert client.get("/api/v1/subscriptions/me").status_code == 401


def test_the_csrf_endpoint_needs_the_cookie(client, notifier):
    signed_in(client, notifier)
    assert client.get("/api/v1/manage/csrf").status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/manage/csrf").status_code == 401


def test_the_api_token_still_works_and_needs_no_form_token(client, notifier, db):
    """The app's credential is unaffected: it cannot be sent by a cross-site request anyway."""
    client.post("/api/v1/subscriptions", json={"email": "app@example.com", "lat": 50.1, "lon": 8.6})
    token = notifier.sent[-1].text.split("token=")[1].split()[0]
    api = client.post("/confirm", data={"token": token}).text.split("<code>")[1].split("</code>")[0]
    headers = {"Authorization": f"Bearer {api}"}
    assert (
        client.patch(
            "/api/v1/subscriptions/me", json={"lead_time_minutes": 45}, headers=headers
        ).status_code
        == 204
    )


# --- what may be changed -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"threshold_mm_5min": 0.001}, "numeric(5,2) rounds it to 0.00 and trips the CHECK"),
        ({"threshold_mm_5min": 41.0}, "above plausibility_max_mm_5min it could never fire"),
        ({"lead_time_minutes": 32}, "rules.py steps by 5, so 32 would be evaluated as 30"),
        ({"lead_time_minutes": 125}, "RV only forecasts 120 minutes"),
        ({"lead_time_minutes": 0}, "below the first forecast frame"),
        ({"radius_m": 20001}, "beyond the radius_sane CHECK"),
    ],
)
def test_values_outside_the_rule_are_refused_readably(client, notifier, body, reason):
    csrf = signed_in(client, notifier)
    response = write(client, csrf, **body)
    # 422 and a sentence, never a 500 from a constraint violation.
    assert response.status_code == 422, reason
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize(
    "body",
    [
        {"threshold_mm_5min": 0.01},  # the RV quantum, the smallest the data can express
        {"threshold_mm_5min": 40.0},  # the plausibility ceiling
        {"lead_time_minutes": 5},
        {"lead_time_minutes": 120},  # the full forecast, as asked for
        {"radius_m": 0},
        {"radius_m": 20000},
    ],
)
def test_the_edges_of_the_rule_are_accepted(client, notifier, body):
    csrf = signed_in(client, notifier)
    assert write(client, csrf, **body).status_code == 204


def test_absent_fields_are_left_alone(client, notifier, db):
    csrf = signed_in(client, notifier)
    write(client, csrf, threshold_mm_5min=0.5, lead_time_minutes=90, radius_m=3000)
    write(client, csrf, lead_time_minutes=15)
    with db() as session:
        row = session.query(Subscription).one()
        assert (float(row.threshold_mm_5min), row.lead_time_minutes, row.radius_m) == (
            0.5,
            15,
            3000,
        )


def test_the_rule_is_writable_but_the_address_is_not(client, notifier):
    csrf = signed_in(client, notifier)
    # Changing where warnings go is a new subscription with its own confirmation, not a setting.
    response = client.patch(
        "/api/v1/subscriptions/me",
        json={"address": "elsewhere@example.com"},
        headers={CSRF_HEADER: csrf},
    )
    assert response.status_code == 422


def test_changing_the_rule_does_not_reset_the_alert_state(client, notifier, db, settings):
    """Only a move does (D-17). Otherwise nudging a number re-arms your own warning."""
    csrf = signed_in(client, notifier)
    with db() as session:
        before = session.query(Subscription).one().location_updated_at
    write(client, csrf, threshold_mm_5min=0.3)
    with db() as session:
        assert session.query(Subscription).one().location_updated_at == before


def test_moving_does_reset_it(client, notifier, db):
    csrf = signed_in(client, notifier)
    with db() as session:
        before = session.query(Subscription).one().location_updated_at
    assert (
        write(
            client,
            csrf,
            method="PUT",
            url="/api/v1/subscriptions/me/location",
            lat=HAMBURG[0],
            lon=HAMBURG[1],
        ).status_code
        == 204
    )
    with db() as session:
        assert session.query(Subscription).one().location_updated_at > before


def test_a_location_outside_germany_is_refused(client, notifier):
    csrf = signed_in(client, notifier)
    response = write(
        client, csrf, method="PUT", url="/api/v1/subscriptions/me/location", lat=40.0, lon=11.0
    )
    assert response.status_code == 422


# --- the page ----------------------------------------------------------------------------------


def test_the_page_renders_for_anyone_and_leaks_nothing(client, notifier):
    subscribed(client, notifier)
    page = client.get("/manage")
    assert page.status_code == 200
    # Identical for everyone: the token is in the fragment, so the server cannot know who this is.
    assert "friend@example.com" not in page.text
    assert MANAGE_COOKIE not in page.text


def test_the_page_renders_the_bounds_it_enforces(client, settings):
    page = client.get("/manage").text
    assert f'max="{settings.max_lead_minutes}"' in page
    assert f'max="{settings.max_radius_m}"' in page
    # The threshold is no longer a free number with a min attribute - it is a dropdown of the
    # map's intensity bands, and test_pages.py checks that list against the map's own.
    assert '<select id="threshold"' in page


def test_the_read_endpoint_publishes_the_bounds(client, notifier, settings):
    signed_in(client, notifier)
    bounds = client.get("/api/v1/subscriptions/me").json()["bounds"]
    assert bounds["lead_max"] == settings.max_lead_minutes
    assert bounds["threshold_min"] == settings.min_threshold_mm_5min
    assert bounds["threshold_max"] == settings.plausibility_max_mm_5min


# --- how long a session lasts, and renewing it ---------------------------------------------------


def test_the_csrf_value_expires_with_the_session_not_on_its_own_clock(client, notifier):
    """They used to drift: every page load minted a fresh thirty minutes for the CSRF token
    while the session's own expiry stayed put, so it could outlive what it belongs to."""
    from rainalert.tokens import verify_csrf_token, verify_session_token

    signed_in(client, notifier)
    state = client.get("/api/v1/manage/csrf").json()
    cookie = client.cookies.get(MANAGE_COOKIE)

    session = verify_session_token(cookie, "test-secret")
    form = verify_csrf_token(state["csrf"], "test-secret")
    assert form.expires == session.expires


def test_the_page_is_told_how_long_is_left(client, notifier, settings):
    signed_in(client, notifier)
    state = client.get("/api/v1/manage/csrf").json()
    assert 0 < state["seconds_left"] <= settings.manage_session_ttl_minutes * 60
    assert state["session_minutes"] == settings.manage_session_ttl_minutes
    assert state["seconds_until_deadline"] <= settings.manage_session_max_minutes * 60


def test_extending_puts_the_session_back_to_full_length(client, notifier, db, settings):
    from rainalert.tokens import session_token, verify_session_token

    signed_in(client, notifier)
    with db() as session:
        subscriber_id = session.query(Subscriber).one().id

    # A session most of the way through its life, with plenty of room before the wall.
    deadline = int((datetime.now(UTC) + timedelta(minutes=90)).timestamp())
    nearly_done = session_token(subscriber_id, settings.secret_key, 2, deadline)
    put_session_cookie(client, nearly_done)
    before = verify_session_token(nearly_done, settings.secret_key).seconds_left()
    assert before <= 120

    fresh = client.get("/api/v1/manage/csrf").json()
    response = client.post("/api/v1/manage/extend", headers={CSRF_HEADER: fresh["csrf"]})
    assert response.status_code == 200
    assert response.json()["seconds_left"] > before
    assert response.json()["seconds_left"] > 60 * (settings.manage_session_ttl_minutes - 1)


def test_extending_cannot_push_past_the_wall(client, notifier, db, settings):
    """Otherwise the renew button turns a deliberately short session into a permanent one."""
    from rainalert.tokens import session_token, verify_session_token

    signed_in(client, notifier)
    with db() as session:
        subscriber_id = session.query(Subscriber).one().id

    # Five minutes left before the wall, which is less than a full session.
    deadline = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    capped = session_token(subscriber_id, settings.secret_key, 30, deadline)
    put_session_cookie(client, capped)

    fresh = client.get("/api/v1/manage/csrf").json()
    response = client.post("/api/v1/manage/extend", headers={CSRF_HEADER: fresh["csrf"]})
    assert response.status_code == 200
    # Renewed, but only up to the wall - not to a full thirty minutes.
    assert response.json()["seconds_left"] <= 5 * 60 + 2
    assert (
        verify_session_token(client.cookies.get(MANAGE_COOKIE), settings.secret_key).deadline
        == deadline
    )


def test_a_session_at_its_wall_is_refused_a_renewal(client, notifier, db, settings):
    from rainalert.tokens import session_token

    signed_in(client, notifier)
    with db() as session:
        subscriber_id = session.query(Subscriber).one().id

    past = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())
    at_the_wall = session_token(subscriber_id, settings.secret_key, 30, past)
    # The token itself is expired once the deadline has passed, so this is a 401 rather than a
    # 409 - either way, no renewal, and the page asks for a new link.
    put_session_cookie(client, at_the_wall)
    fresh = client.get("/api/v1/manage/csrf")
    assert fresh.status_code == 401


def test_extending_needs_the_form_token(client, notifier):
    """Extending a credential's life is a write, and exactly what another site would like."""
    signed_in(client, notifier)
    assert client.post("/api/v1/manage/extend").status_code == 403


def test_a_tampered_cookie_is_refused(client, notifier, settings):
    """The holder can read and delete the cookie; they cannot alter it.

    Everything before the signature is inside the signature, so pushing the expiry out, swapping
    the subscriber id, or promoting a session token to a CSRF token all fail verification.
    """
    signed_in(client, notifier)
    good = client.cookies.get(MANAGE_COOKIE)
    purpose, ident, expires, deadline, mac = good.split(".")

    for label, forged in {
        "expiry pushed out": f"{purpose}.{ident}.{int(expires) + 31536000}.{deadline}.{mac}",
        "wall pushed out": f"{purpose}.{ident}.{expires}.{int(deadline) + 31536000}.{mac}",
        "someone else's id": f"{purpose}.{uuid.uuid4()}.{expires}.{deadline}.{mac}",
        "promoted to csrf": f"csrf.{ident}.{expires}.{deadline}.{mac}",
        "signature dropped": f"{purpose}.{ident}.{expires}.{deadline}.",
    }.items():
        put_session_cookie(client, forged)
        assert client.get("/api/v1/subscriptions/me").status_code == 401, label
