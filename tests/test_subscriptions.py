"""Subscription lifecycle and the HTTP layer around it.

Weighted towards the things that are invisible when they break: double opt-in as an anti-relay
control, scanner-safe links, enumeration, and rate limiting that cannot be spoofed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rainalert import subscriptions as svc
from rainalert.api.app import create_app
from rainalert.config import Settings
from rainalert.db.models import AuthToken, RateLimitHit, Subscriber, Subscription, TokenPurpose
from rainalert.notify import ConsoleNotifier

FRANKFURT = (50.1109, 8.6821)


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
    app = create_app(settings, session_factory=db, notifier=notifier)
    return TestClient(app)


def subscribe(client, email="friend@example.com", lat=FRANKFURT[0], lon=FRANKFURT[1]):
    return client.post("/api/v1/subscriptions", json={"email": email, "lat": lat, "lon": lon})


def confirm_token_from(notifier) -> str:
    body = notifier.sent[-1].text
    return body.split("/confirm#t=")[1].split()[0]


# --- double opt-in ---------------------------------------------------------------------------


def test_subscribe_sends_a_confirmation_and_stores_nothing_active(client, notifier, db):
    assert subscribe(client).status_code == 202
    assert len(notifier.sent) == 1
    assert notifier.sent[0].to == "friend@example.com"
    with db() as session:
        assert session.query(Subscriber).one().confirmed_at is None
        assert session.query(Subscription).one().status.value == "pending"


def test_confirm_activates_and_issues_a_token_once(client, notifier, db):
    subscribe(client)
    token = confirm_token_from(notifier)
    response = client.post("/confirm", data={"token": token})
    assert response.status_code == 200
    assert "Angemeldet" in response.text
    with db() as session:
        assert session.query(Subscriber).one().confirmed_at is not None
        assert session.query(Subscription).one().status.value == "active"
    # single use
    assert (
        "nicht gültig"
        in client.post("/confirm", data={"token": token}).text.replace(
            "nicht gueltig", "nicht gültig"
        )
        or client.post("/confirm", data={"token": token}).status_code == 400
    )


def test_get_confirm_changes_nothing(client, notifier, db):
    """Mail scanners follow links. A state-changing GET would consume the token before the
    user ever clicks, and hand the scanner the bearer token (SECURITY_REVIEW.md F-4)."""
    subscribe(client)
    token = confirm_token_from(notifier)
    page = client.get("/confirm")
    assert page.status_code == 200
    with db() as session:
        assert session.query(Subscriber).one().confirmed_at is None  # untouched
    assert "Zugangsschlüssel" not in page.text  # no bearer token handed out on GET
    assert client.post("/confirm", data={"token": token}).status_code == 200  # still usable


def test_never_mails_an_address_that_did_not_confirm(client, notifier, db):
    """The anti-relay property: nothing is ever sent to an address beyond one confirmation."""
    for _ in range(3):
        subscribe(client, email="victim@example.com")
    assert all(m.to == "victim@example.com" for m in notifier.sent)
    assert all(
        "bestaetigen" in m.subject.lower() or "best" in m.subject.lower() for m in notifier.sent
    )


def test_resubscribing_supersedes_the_previous_token(client, notifier, db):
    subscribe(client)
    first = confirm_token_from(notifier)
    subscribe(client)
    second = confirm_token_from(notifier)
    assert first != second
    assert client.post("/confirm", data={"token": first}).status_code == 400
    assert client.post("/confirm", data={"token": second}).status_code == 200


def test_expired_confirmation_is_refused(db, settings, notifier):
    with db() as session:
        result = svc.subscribe(
            session, settings, address="a@example.com", lat=FRANKFURT[0], lon=FRANKFURT[1]
        )
        later = datetime.now(UTC) + timedelta(hours=settings.confirm_token_ttl_hours + 1)
        with pytest.raises(svc.ValidationError, match="expired"):
            svc.confirm(session, settings, token=result.confirm_token, now=later)


# --- enumeration -----------------------------------------------------------------------------


def test_response_is_identical_for_known_and_unknown_addresses(client, notifier):
    first = subscribe(client, email="known@example.com")
    client.post("/confirm", data={"token": confirm_token_from(notifier)})
    before = len(notifier.sent)

    again = subscribe(client, email="known@example.com")
    fresh = subscribe(client, email="brand-new@example.com")

    assert first.status_code == again.status_code == fresh.status_code == 202
    assert again.json() == fresh.json() == first.json()
    # ...and the already-confirmed address gets no second mail
    assert len(notifier.sent) == before + 1


# --- authenticated API -----------------------------------------------------------------------


def api_token_from(page_text: str) -> str:
    return page_text.split("<code>")[1].split("</code>")[0].strip()


def test_me_update_and_delete(client, notifier, db):
    subscribe(client)
    page = client.post("/confirm", data={"token": confirm_token_from(notifier)})
    token = api_token_from(page.text)
    auth = {"Authorization": f"Bearer {token}"}

    me = client.get("/api/v1/subscriptions/me", headers=auth)
    assert me.status_code == 200
    assert me.json()["status"] == "active"
    assert me.json()["lead_time_minutes"] == 30  # D-13 default

    moved = client.put(
        "/api/v1/subscriptions/me/location", json={"lat": 53.5511, "lon": 9.9937}, headers=auth
    )
    assert moved.status_code == 204
    assert client.get("/api/v1/subscriptions/me", headers=auth).json()["lat"] == pytest.approx(
        53.5511
    )

    assert client.delete("/api/v1/subscriptions/me", headers=auth).status_code == 204
    with db() as session:
        assert session.query(Subscriber).count() == 0
        assert session.query(Subscription).count() == 0
        assert session.query(AuthToken).count() == 0
    assert client.get("/api/v1/subscriptions/me", headers=auth).status_code == 401


def test_unauthenticated_and_bad_tokens_are_refused(client):
    assert client.get("/api/v1/subscriptions/me").status_code == 401
    assert (
        client.get("/api/v1/subscriptions/me", headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )


@pytest.mark.parametrize(
    "body",
    [
        # NaN is not in this list because httpx refuses to serialise it into the request at all;
        # it arrives as a raw body instead - see test_nan_as_a_bare_json_token_is_refused.
        {"email": "a@example.com", "lat": 91.0, "lon": 8.0},
        {"email": "a@example.com", "lat": 41.9, "lon": 12.5},  # Rome: outside coverage
        {"email": "not-an-email", "lat": 50.0, "lon": 8.0},
        {"email": "a@example.com", "lat": 50.0, "lon": 8.0, "extra": "field"},
    ],
)
def test_bad_input_is_refused_at_the_edge(client, body, db):
    """A location that cannot be evaluated must never reach the database: it would be retried
    every cycle forever (SECURITY_REVIEW.md F-3)."""
    response = client.post("/api/v1/subscriptions", json=body)
    assert response.status_code in (400, 422)
    with db() as session:
        assert session.query(Subscriber).count() == 0


def test_nan_as_a_bare_json_token_is_refused(client, db):
    """json.loads accepts the bare NaN token, so this arrives without any exotic tooling."""
    response = client.post(
        "/api/v1/subscriptions",
        content='{"email": "a@example.com", "lat": NaN, "lon": 8.0}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code in (400, 422)
    with db() as session:
        assert session.query(Subscriber).count() == 0


# --- unsubscribe -----------------------------------------------------------------------------


def test_get_unsubscribe_changes_nothing_but_post_deletes(client, notifier, db):
    """A prefetching client must not be able to delete someone's account."""
    subscribe(client)
    page = client.post("/confirm", data={"token": confirm_token_from(notifier)})
    unsub = page.text.split("/unsubscribe#t=")[1].split("<")[0].strip()

    assert client.get("/unsubscribe").status_code == 200
    with db() as session:
        assert session.query(Subscriber).count() == 1  # still there

    assert client.post("/unsubscribe", data={"token": unsub}).status_code == 200
    with db() as session:
        assert session.query(Subscriber).count() == 0


# --- rate limiting ---------------------------------------------------------------------------


def test_subscribe_is_rate_limited(client, settings):
    for _ in range(settings.subscribe_limit_per_hour):
        assert subscribe(client, email="a@example.com").status_code == 202
    assert subscribe(client, email="a@example.com").status_code == 429


def test_x_forwarded_for_cannot_be_spoofed_when_no_proxy_is_trusted(client, settings):
    """trusted_proxy_hops is 0 here, so the header is ignored entirely and the limit still bites."""
    assert settings.trusted_proxy_hops == 0
    for i in range(settings.subscribe_limit_per_hour):
        assert subscribe(client, email=f"u{i}@example.com").status_code == 202
    spoofed = client.post(
        "/api/v1/subscriptions",
        json={"email": "u99@example.com", "lat": FRANKFURT[0], "lon": FRANKFURT[1]},
        headers={"X-Forwarded-For": "1.2.3.4"},
    )
    assert spoofed.status_code == 429


def test_deleting_an_account_does_not_erase_the_abuse_record(client, notifier, db):
    """Otherwise delete-and-retry resets every limit (SECURITY_REVIEW.md F-5)."""
    subscribe(client)
    page = client.post("/confirm", data={"token": confirm_token_from(notifier)})
    client.delete(
        "/api/v1/subscriptions/me",
        headers={"Authorization": f"Bearer {api_token_from(page.text)}"},
    )
    with db() as session:
        assert session.query(Subscriber).count() == 0
        assert session.query(RateLimitHit).count() > 0


# --- hygiene ---------------------------------------------------------------------------------


def test_security_headers_are_present(client):
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"


def test_unconfirmed_signups_are_purged(db, settings):
    with db() as session:
        svc.subscribe(
            session, settings, address="ghost@example.com", lat=FRANKFURT[0], lon=FRANKFURT[1]
        )
        later = datetime.now(UTC) + timedelta(hours=settings.unconfirmed_purge_hours + 1)
        assert svc.purge_unconfirmed(session, settings, now=later) == 1
        assert session.query(Subscriber).count() == 0


def test_tokens_are_never_stored_in_plaintext(db, settings, notifier):
    with db() as session:
        result = svc.subscribe(
            session, settings, address="a@example.com", lat=FRANKFURT[0], lon=FRANKFURT[1]
        )
        stored = session.query(AuthToken).one()
        assert result.confirm_token.encode() not in bytes(stored.token_hash)
        assert len(stored.token_hash) == 32
        confirmed = svc.confirm(session, settings, token=result.confirm_token)
        hashes = {bytes(t.token_hash) for t in session.query(AuthToken).all()}
        assert confirmed.api_token.encode() not in b"".join(hashes)
        assert all(t.purpose in set(TokenPurpose) for t in session.query(AuthToken).all())


def test_a_pasted_coordinate_is_accepted_and_reduced(client, db):
    """Seven decimals from a mapping site must not be refused - or stored.

    The form used to carry step="0.0001", so the browser rejected the value before it was ever
    sent. That put the rounding in the one place it could not be enforced, and made a legitimate
    paste look like a typo.
    """
    r = client.post(
        "/api/v1/subscriptions",
        json={"email": "paste@example.com", "lat": 48.153330, "lon": 11.557428},
    )
    assert r.status_code in (200, 201, 202), r.text

    with db() as session:
        sub = session.execute(select(Subscription)).scalars().one()
        assert (sub.lat, sub.lon) == (48.1533, 11.5574)


def test_precision_beyond_the_policy_is_never_stored(client, db):
    """The minimisation has to hold for a caller who skips the form entirely."""
    client.post(
        "/api/v1/subscriptions",
        json={"email": "precise@example.com", "lat": 48.15333012345, "lon": 11.55742898765},
    )
    with db() as session:
        sub = session.execute(select(Subscription)).scalars().one()
        # Storing 1 cm of someone's location for a service that samples a 1 km grid is
        # collecting what cannot be used.
        assert len(str(sub.lat).split(".")[1]) <= 4
        assert len(str(sub.lon).split(".")[1]) <= 4


def test_the_form_does_not_constrain_decimals(client):
    body = client.get("/").text
    assert 'step="any"' in body
    assert 'name="lat" step="0.0001"' not in body


def test_no_message_we_send_puts_a_token_in_a_query_string(client, notifier, db):
    """D-26, applied to the two links that still broke it.

    uvicorn and Cloud Run both log the full request URL, so `?token=` hands the credential to
    the log; a fragment is never sent to the server at all. Asserted over the message bodies
    rather than per-link, so a third link added later is covered without anyone remembering to
    come back here.
    """
    subscribe(client)
    client.post("/confirm", data={"token": confirm_token_from(notifier)})

    for message in notifier.sent:
        assert "?token=" not in message.text, f"{message.subject} leaks a token in a query string"
        assert "?token=" not in (message.click_url or "")


def test_the_confirmation_link_is_tappable_and_carries_the_token_in_the_fragment(
    client, notifier, db
):
    subscribe(client)
    message = notifier.sent[-1]
    assert "/confirm#t=" in message.text
    # Push has nowhere to put a link except the Click header, so it has to carry the same shape.
    assert (message.click_url or "").startswith("http")
    assert "#t=" in (message.click_url or "")


def test_the_query_parameter_is_gone_rather_than_deprecated(client, db):
    """Accepting `?token=` as well would leave the logged shape working for anyone who still
    sent one - a second code path kept alive for data that only ever existed in development."""
    for path in ("/confirm", "/unsubscribe"):
        page = client.get(f"{path}?token=would-have-worked-before")
        assert page.status_code == 200
        assert "would-have-worked-before" not in page.text


def test_the_pages_say_so_when_a_link_arrives_without_its_token(client, db):
    """A bare /confirm is what a mail scanner or a truncated link produces. The button must not
    sit there looking ready to work."""
    for path in ("/confirm", "/unsubscribe"):
        page = client.get(path)
        assert page.status_code == 200
        assert 'id="no-token"' in page.text
        # The fragment is read by script, so the page needs the nonce to be allowed to run.
        assert "csp_nonce" not in page.text  # rendered, not left as a literal
        assert "<script nonce=" in page.text
