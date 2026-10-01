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
from rainalert.tokens import (
    csrf_token,
    session_token,
    verify_csrf_token,
    verify_manage_request_token,
)
from tests.helpers import js_function

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
    """A confirmed, active email subscriber - and *only* that.

    Confirming now also opens a session, which is the point of that change but would quietly
    wreck every test below it: they would start authorised, and a magic link that had stopped
    working would still look like it worked. So the cookie goes, and the assertion holds this
    helper to its own docstring.
    """
    client.post("/api/v1/subscriptions", json={"email": email, "lat": lat, "lon": lon})
    token = notifier.sent[-1].text.split("/confirm#")[1].split("=", 1)[1].split()[0]
    client.post("/confirm", data={"token": token})
    client.cookies.delete(MANAGE_COOKIE)
    assert client.get("/api/v1/subscriptions/me").status_code == 401
    return email


def link_token(notifier) -> str:
    """The magic link puts its token in the fragment, not the query string.

    Read from the body for email and from `click_url` for push, because the push branch no longer
    prints a URL at all - a notification body is plain text nothing linkifies, and the mail version
    of this message put two untappable URLs and a licence footer in a notification shade. The rule
    being checked is unchanged and is the point of the helper: wherever the token is, it is behind a
    `#` (F-4/F-8), never in a query string a log or a Referer header would keep.
    """
    message = notifier.sent[-1]
    carrier = message.text if "/manage#t=" in message.text else (message.click_url or "")
    assert "/manage#t=" in carrier, "the token must ride in the fragment (F-4/F-8)"
    assert "?t=" not in carrier and "?token=" not in carrier
    return carrier.split("/manage#t=")[1].split()[0]


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
    token = notifier.sent[-1].text.split("/confirm#")[1].split("=", 1)[1].split()[0]
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


# --- getting in without typing anything ---------------------------------------------------


PUSH_ENDPOINT = "https://fcm.googleapis.com/fcm/send/manage-test-endpoint"


def push_subscribed(client, notifier, lat=MUNICH[0], lon=MUNICH[1], endpoint=PUSH_ENDPOINT):
    """A confirmed push subscriber, still signed in - which is the behaviour under test.

    The endpoint is passed in rather than read back from the response: the subscribe endpoint no
    longer echoes anything, because the browser already holds what it just gave us. Under ntfy it
    had to return the topic, which is what this helper used to read.
    """
    client.post(
        "/api/v1/subscriptions",
        json={
            "channel": "webpush",
            "lat": lat,
            "lon": lon,
            "endpoint": endpoint,
            "p256dh": "k" * 87,
            "auth": "a" * 22,
        },
    )
    token = notifier.sent[-1].click_url.split("/confirm#")[1].split("=", 1)[1]
    client.post("/confirm", data={"token": token})
    return endpoint


def test_confirming_leaves_a_working_session(client, notifier, db):
    """Confirming proves the same thing redeeming a magic link proves - a token we sent to the
    channel came back - so a second link to prove it again is ceremony."""
    client.post(
        "/api/v1/subscriptions",
        json={"email": "new@example.com", **dict(zip(("lat", "lon"), MUNICH))},
    )
    token = notifier.sent[-1].text.split("/confirm#")[1].split("=", 1)[1].split()[0]
    confirmed = client.post("/confirm", data={"token": token})

    assert confirmed.status_code == 200
    assert MANAGE_COOKIE in confirmed.cookies
    me = client.get("/api/v1/subscriptions/me")
    assert me.status_code == 200
    assert me.json()["address"] == "new@example.com"


def test_the_session_from_confirming_can_write_and_is_not_open_ended(client, notifier, db):
    client.post(
        "/api/v1/subscriptions",
        json={"email": "new@example.com", "lat": MUNICH[0], "lon": MUNICH[1]},
    )
    token = notifier.sent[-1].text.split("/confirm#")[1].split("=", 1)[1].split()[0]
    client.post("/confirm", data={"token": token})

    state = client.get("/api/v1/manage/csrf").json()
    assert write(client, state["csrf"], threshold_mm_5min=0.25).status_code == 204
    # The ordinary session, not a longer one bought by arriving a different way.
    assert state["seconds_left"] <= 30 * 60
    assert state["seconds_until_deadline"] <= 120 * 60


def test_confirming_sends_nothing_after_the_confirmation_on_either_channel(client, notifier, db):
    """The anchor message is gone (D-45).

    It existed because an ntfy topic was an unmemorable string the reader had to keep somewhere, so
    the notification itself was the bookmark - "Behalte diese Nachricht". A web push notification
    cannot be a bookmark: it is gone the moment it is swiped, and Android keeps no history by
    default. What replaces it is the session cookie this confirmation sets, plus an Einstellungen
    button on every warning.
    """
    before = len(notifier.sent)
    push_subscribed(client, notifier)
    # Exactly one: the confirmation. Nothing follows it.
    assert len(notifier.sent) == before + 1

    before = len(notifier.sent)
    subscribed(client, notifier)
    assert len(notifier.sent) == before + 1


def test_confirming_a_push_subscription_leaves_a_working_session(client, notifier, db):
    """The replacement for the anchor message, and the reason it is not needed: confirming proves
    the channel reached this browser, which is what a magic link proves, so the session starts
    here."""
    push_subscribed(client, notifier)
    assert client.get("/api/v1/subscriptions/me").status_code == 200


def request_token(notifier, client=None, db=None) -> str:
    """The durable token the Einstellungen button carries.

    Minted here rather than read out of a message. It used to be read off the anchor notification,
    which was the one message that reliably held one; now it rides on every warning, and a test that
    wanted one had to provoke rain. `settings_action` is the same builder the notification uses, so
    what is exercised downstream is unchanged.
    """
    from rainalert.db.models import Subscriber
    from rainalert.tokens import manage_request_token

    with db() as session:
        subscriber = session.query(Subscriber).one()
        return manage_request_token(subscriber.id, "test-secret", 365)


def test_the_button_sends_the_magic_link_to_the_same_browser(client, notifier, db):
    endpoint = push_subscribed(client, notifier)
    token = request_token(notifier, client, db)

    response = client.post("/api/v1/manage/request", json={"token": token})
    assert response.status_code == 202
    assert notifier.sent[-1].to == endpoint
    # `click_url` on push, not the body: the push branch of `manage_link_message` is one line now,
    # because the mail body put two untappable URLs and a licence footer in a notification shade.
    assert "/manage#t=" in (notifier.sent[-1].click_url or "")

    # And that link is the ordinary one, so it still opens the ordinary session.
    client.cookies.delete(MANAGE_COOKIE)
    opened = client.post("/api/v1/manage/session", data={"token": link_token(notifier)})
    assert opened.status_code == 200


def test_the_button_can_be_used_more_than_once(client, notifier, db):
    """The durable token is not spent by using it - the short-lived link it mints is."""
    push_subscribed(client, notifier)
    token = request_token(notifier, client, db)

    assert client.post("/api/v1/manage/request", json={"token": token}).status_code == 202
    assert client.post("/api/v1/manage/request", json={"token": token}).status_code == 202


def test_the_request_token_cannot_itself_open_a_session(client, notifier, db):
    """The button asks; it does not admit. That split is what lets it be durable enough to sit
    in a notification the reader keeps."""
    push_subscribed(client, notifier)
    token = request_token(notifier, client, db)
    client.cookies.delete(MANAGE_COOKIE)

    assert client.post("/api/v1/manage/session", data={"token": token}).status_code == 401
    assert client.get("/api/v1/subscriptions/me").status_code == 401


@pytest.mark.parametrize("token", ["", "nonsense", "request.not-a-uuid.1.1.x"])
def test_a_token_that_does_not_verify_is_answered_the_same_as_one_that_does(
    client, notifier, db, token
):
    """Never "that token is invalid": the answer must not tell a holder whether the
    subscription behind an expired token still exists."""
    push_subscribed(client, notifier)
    before = len(notifier.sent)

    response = client.post("/api/v1/manage/request", json={"token": token})
    assert response.status_code == 202
    assert len(notifier.sent) == before


def test_a_token_for_a_deleted_subscriber_sends_nothing(client, notifier, db):
    push_subscribed(client, notifier)
    token = request_token(notifier, client, db)
    state = client.get("/api/v1/manage/csrf").json()
    assert write(client, state["csrf"], method="DELETE").status_code == 204
    before = len(notifier.sent)

    assert client.post("/api/v1/manage/request", json={"token": token}).status_code == 202
    assert len(notifier.sent) == before


def test_the_button_is_capped_per_subscriber(client, notifier, db, settings):
    """Per subscriber, not only per IP: the button is tapped from whatever network the phone is
    on, so an IP counter alone would be counting the wrong thing."""
    push_subscribed(client, notifier)
    token = request_token(notifier, client, db)

    for _ in range(settings.manage_request_limit_per_hour):
        assert client.post("/api/v1/manage/request", json={"token": token}).status_code == 202
    assert client.post("/api/v1/manage/request", json={"token": token}).status_code == 429


def test_a_valid_token_of_another_purpose_is_not_accepted_by_the_button(client, notifier, db):
    """Garbage is the easy half. The half that matters is a token that verifies perfectly -
    just as something else - which is why the purpose is inside the MAC and not a prefix."""
    push_subscribed(client, notifier)
    who = verify_manage_request_token(
        request_token(notifier, client, db), "test-secret"
    ).subscriber_id
    before = len(notifier.sent)

    for wrong in (
        session_token(who, "test-secret", 30),
        csrf_token(who, "test-secret", int(datetime.now(UTC).timestamp()) + 600),
    ):
        assert client.post("/api/v1/manage/request", json={"token": wrong}).status_code == 202
        assert len(notifier.sent) == before, "a token minted for something else was accepted"


def test_the_settings_page_starts_on_none_of_its_states(client):
    """Four states, and the served markup commits to none of them.

    The page used to render the gate as its default. That made the form the thing you looked at
    while any other path was still working - most visibly arriving from a notification, where
    "we sent you a link" appeared *underneath* a form asking for the topic it had just used.
    """
    body = client.get("/manage").text

    def opening_tag(marker):
        start = body.index(marker)
        return body[body.rindex("<", 0, start) : body.index(">", start) + 1]

    for marker in ('id="sent"', 'id="gate"', 'id="panel"'):
        assert "hidden" in opening_tag(marker), f"{marker} must start hidden: {opening_tag(marker)}"
    # ...and something honest is on screen until the script decides.
    assert 'id="busy"' in body and "spinner" in body


def test_the_session_cookie_is_named_what_the_cdn_lets_through():
    """Firebase Hosting strips every cookie except one named `__session`.

    It fronts this service because Cloud Run has no domain mapping in europe-west3, and it drops
    all other cookies from proxied requests so that it can cache: the `__session` cookie goes into
    the cache key, so two visitors with different sessions cannot be served each other's response.

    Asserted by name because the failure is silent and misleading. With any other name the magic
    link redeems, the cookie is set, and then every request that needs it arrives without one -
    `GET /api/v1/subscriptions/me` answers 401 and the settings page says "Deine Einstellungen
    konnten gerade nicht geladen werden", which reads as a server fault and sends the reader to
    request another link that fails identically. That is what happened on the first cutover.

    If this service ever stops being served through Hosting, this constraint goes with it - but
    then this test is the thing that says so, rather than a rename nobody connects to a CDN.
    """
    from rainalert.api.app import MANAGE_COOKIE

    assert MANAGE_COOKIE == "__session", (
        "Firebase Hosting will strip any other name and the settings page will 401 with no error "
        "anywhere - see this test's docstring"
    )


def test_the_session_cookie_actually_round_trips(client, notifier):
    """And the name is not enough on its own: it has to be the one the app reads back.

    Two constants could disagree - one used to set the cookie and one to read it - and the name
    assertion above would pass while nothing worked. This drives the real flow instead: redeem a
    link, then use the session it opened.
    """
    subscribed(client, notifier)
    # A magic link has to be requested first; the notification `subscribed` leaves behind is the
    # confirmation, whose token goes to /confirm rather than /manage.
    client.post("/api/v1/manage/link", json={"channel": "email", "address": "friend@example.com"})
    response = client.post("/api/v1/manage/session", data={"token": link_token(notifier)})
    assert response.status_code == 200, response.text
    assert "__session" in response.cookies, dict(response.cookies)
    # The client carries the cookie forward, which is the part that matters.
    assert client.get("/api/v1/subscriptions/me").status_code == 200


def test_every_exit_from_the_settings_dispatch_names_a_state(client):
    """The old code relied on the gate being the default, so several paths just `return`ed and
    left whatever happened to be on screen. With nothing shown by default that is a blank page,
    so each one has to say what it wants.

    Two assertions rather than one loose scan: a single "did some function get called near this
    return" check has to accept `redeem`, and then it is only an allowlist of names. So the
    dispatch's own returns are checked here, and `redeem` - the one exit that decides elsewhere -
    is pinned by the test below.
    """
    body = client.get("/manage").text
    dispatch = js_function(body, "start")

    deciders = ("show(STATES", "gateWithNote(", "requestLink(", "redeem(")
    for chunk in dispatch.split("return;")[:-1]:
        tail = chunk[-400:]
        assert any(d in tail for d in deciders), tail


def test_a_spent_link_falls_through_to_a_session_this_browser_already_has(client):
    """Opening the settings link twice in one browser is not a dead end.

    The first open spends the token *and* sets the session cookie, so the second answered
    "dieser Link gilt nicht mehr" to somebody who was signed in - and reloading that same page
    then worked, which is the part that makes it baffling rather than merely wrong. `redeem`
    now reports; only the dispatch decides, and only once the session has also been ruled out.
    """
    body = client.get("/manage").text

    redeem = body[body.index("async function redeem") : body.index("async function load")]
    assert "gateWithNote(" not in redeem, "redeem must report, not decide: " + redeem

    start = js_function(body, "start")
    spent_at = start.index("spent = !await redeem(token)")
    session_at = start.index("/api/v1/manage/csrf")
    complaint_at = start.index("gilt nicht mehr")
    # The session is consulted between the failed redemption and the complaint about it.
    assert spent_at < session_at < complaint_at, start
    # ...and the complaint is reached only when that session lookup fails.
    assert "if (!again.ok)" in start[session_at:complaint_at], start[session_at:complaint_at]


def test_a_spent_link_that_still_opens_says_so(client):
    """Otherwise the second tab looks identical to the first and the earlier confusion just
    becomes silent. Its own element, because fill() owns #panel-banner and the two would
    overwrite each other."""
    body = client.get("/manage").text
    assert 'id="panel-note"' in body
    note = body[body.index('id="panel-note"') :]
    assert "hidden" in note[: note.index(">")]
    assert "schon benutzt" in body


def test_a_failed_link_request_does_not_blame_the_reader_for_our_fault(client):
    """429 and 500 used to give the same answer, which sent somebody away for an hour over a
    fault on our side."""
    body = client.get("/manage").text
    request_fn = body[body.index("async function requestLink") : body.index("async function start")]
    assert "response.status === 429" in request_fn
    assert "schiefgegangen" in request_fn


def test_the_settings_page_says_why_nothing_happens_without_script(client):
    body = client.get("/manage").text
    assert "<noscript>" in body
    assert "JavaScript" in body[body.index("<noscript>") : body.index("</noscript>")]


@pytest.fixture()
def behind_proxy(db, settings, notifier):
    """A client whose `X-Forwarded-For` is trusted, so a test can rotate the apparent source IP.

    The default `trusted_proxy_hops=0` ignores the header - correctly, since believing it unproven is
    how a client spoofs its own identity (SECURITY_REVIEW.md F-5). Production runs behind Cloud Run,
    which does set it, so this is the configuration the per-IP limiter actually faces.
    """
    proxied = settings.model_copy(update={"trusted_proxy_hops": 1})
    return TestClient(
        create_app(proxied, session_factory=db, notifier=notifier),
        base_url=proxied.public_base_url,
    )


def test_a_settings_link_cannot_be_flooded_from_many_ips(behind_proxy, settings, notifier):
    """The per-address half of the limiter, which was missing while the setting that configures it
    described itself as "deliberately as tight as signing up".

    It was not as tight: `POST /api/v1/subscriptions` limits per-IP *and* per-address, and this route
    limited only per-IP - so the half that survives IP rotation was the half absent. Demonstrated
    before the fix: 40 POSTs carrying 40 different `X-Forwarded-For` values, all 202, 40 messages
    delivered to one subscriber.

    The flood is the lesser harm. `issue_manage_token` deletes the subscriber's previous *unused*
    token, so a stranger who knows an address could invalidate that person's real settings link as
    fast as they could ask for one - and for a push subscriber the settings page is the only route to
    "Abmelden und meine Daten löschen", so their deletion right could be held shut indefinitely.
    """
    email = subscribed(behind_proxy, notifier)
    notifier.sent.clear()

    codes = [
        behind_proxy.post(
            "/api/v1/manage/link",
            json={"channel": "email", "address": email},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        ).status_code
        for i in range(settings.manage_link_limit_per_hour * 4)
    ]

    assert codes.count(202) == settings.manage_link_limit_per_hour, (
        "rotating the client IP must not buy more settings links for one address"
    )
    assert 429 in codes
    assert len(notifier.sent) == settings.manage_link_limit_per_hour


def test_the_address_limiter_is_not_an_existence_oracle(behind_proxy, settings, notifier):
    """An address nobody has ever used must be counted and refused exactly like a real one.

    Otherwise the limiter itself answers the question the route is careful not to: this endpoint
    returns 202 whether or not the address is known, and a 429 that only ever appeared for real
    subscribers would undo that.
    """
    codes = [
        behind_proxy.post(
            "/api/v1/manage/link",
            json={"channel": "email", "address": "nobody@example.invalid"},
            headers={"X-Forwarded-For": f"198.51.100.{i}"},
        ).status_code
        for i in range(settings.manage_link_limit_per_hour * 2)
    ]
    assert codes.count(202) == settings.manage_link_limit_per_hour
    assert 429 in codes
    assert notifier.sent == [], "nothing may be sent for an address nobody subscribed"
