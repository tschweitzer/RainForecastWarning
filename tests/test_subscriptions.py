"""Subscription lifecycle and the HTTP layer around it.

Weighted towards the things that are invisible when they break: double opt-in as an anti-relay
control, scanner-safe links, enumeration, and rate limiting that cannot be spoofed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rainalert import subscriptions as svc
from rainalert.api.app import CSRF_HEADER, create_app
from rainalert.config import Settings
from rainalert.db.models import AuthToken, RateLimitHit, Subscriber, Subscription, TokenPurpose
from rainalert.notify import ConsoleNotifier

FRANKFURT = (50.1109, 8.6821)
P256DH = "k" * 87
AUTH = "a" * 22


def endpoint(name: str = "friend") -> str:
    return f"https://fcm.googleapis.com/fcm/send/{name}"


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
def notifier():
    return ConsoleNotifier()


@pytest.fixture()
def client(db, settings, notifier):
    app = create_app(settings, session_factory=db, notifier=notifier)
    # https, as `public_base_url` is: the session cookie is `Secure` then, and confirming opens one.
    return TestClient(app, base_url="https://testserver")


def push_body(name="friend", lat=FRANKFURT[0], lon=FRANKFURT[1], **extra):
    return {
        "lat": lat,
        "lon": lon,
        "endpoint": endpoint(name),
        "p256dh": P256DH,
        "auth": AUTH,
    } | extra


def subscribe(client, name="friend", lat=FRANKFURT[0], lon=FRANKFURT[1], **kwargs):
    return client.post("/api/v1/subscriptions", json=push_body(name, lat, lon), **kwargs)


def svc_subscribe(session, settings, name="friend"):
    return svc.subscribe(
        session,
        settings,
        endpoint=endpoint(name),
        push_p256dh=P256DH,
        push_auth=AUTH,
        lat=FRANKFURT[0],
        lon=FRANKFURT[1],
    )


def confirm_token_from(notifier) -> str:
    return notifier.sent[-1].click_url.split("/confirm#a=")[1]


def csrf(client) -> str:
    """The value a write under the cookie session needs (F-16). Confirming opened the session."""
    return client.get("/api/v1/manage/csrf").json()["csrf"]


# --- double opt-in ---------------------------------------------------------------------------


def test_subscribe_sends_a_confirmation_and_stores_nothing_active(client, notifier, db):
    assert subscribe(client).status_code == 202
    assert len(notifier.sent) == 1
    assert notifier.sent[0].to == endpoint()
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
    """Link scanners follow links. A state-changing GET would consume the token before the user
    ever taps it (SECURITY_REVIEW.md F-4)."""
    subscribe(client)
    token = confirm_token_from(notifier)
    page = client.get("/confirm")
    assert page.status_code == 200
    with db() as session:
        assert session.query(Subscriber).one().confirmed_at is None  # untouched
    assert client.post("/confirm", data={"token": token}).status_code == 200  # still usable


def test_an_endpoint_that_did_not_confirm_gets_only_confirmations(client, notifier, db):
    """The anti-relay property: nothing is ever sent to an endpoint beyond its confirmation."""
    for _ in range(3):
        subscribe(client, name="victim")
    assert all(m.to == endpoint("victim") for m in notifier.sent)
    assert all("bestätigen" in m.subject.lower() for m in notifier.sent)


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
        result = svc_subscribe(session, settings)
        later = datetime.now(UTC) + timedelta(hours=settings.confirm_token_ttl_hours + 1)
        with pytest.raises(svc.ValidationError, match="expired"):
            svc.confirm(session, settings, token=result.confirm_token, now=later)


# --- a confirmed browser signing up again -----------------------------------------------------


def test_a_confirmed_browser_signing_up_again_gets_no_second_confirmation(client, notifier):
    """It is told so instead, because no notification is coming and the page would wait for one
    forever. That answer is safe to give: the endpoint is a capability only that browser and its
    push service hold, and `subscribe` only moves the location for its owner."""
    subscribe(client)
    client.post("/confirm", data={"token": confirm_token_from(notifier)})
    before = len(notifier.sent)

    again = subscribe(client)
    assert again.status_code == 202
    assert again.json()["already_active"] is True
    assert len(notifier.sent) == before
    assert "already_active" not in subscribe(client, name="brand-new").json()


# --- the settings API under the cookie session ---------------------------------------------


def test_me_update_and_delete(client, notifier, db):
    subscribe(client)
    client.post("/confirm", data={"token": confirm_token_from(notifier)})
    headers = {CSRF_HEADER: csrf(client)}

    me = client.get("/api/v1/subscriptions/me")
    assert me.status_code == 200
    assert me.json()["status"] == "active"
    assert me.json()["lead_time_minutes"] == 30  # D-13 default

    moved = client.put(
        "/api/v1/subscriptions/me/location", json={"lat": 53.5511, "lon": 9.9937}, headers=headers
    )
    assert moved.status_code == 204
    assert client.get("/api/v1/subscriptions/me").json()["lat"] == pytest.approx(53.5511)

    assert client.delete("/api/v1/subscriptions/me", headers=headers).status_code == 204
    with db() as session:
        assert session.query(Subscriber).count() == 0
        assert session.query(Subscription).count() == 0
        assert session.query(AuthToken).count() == 0
    assert client.get("/api/v1/subscriptions/me").status_code == 401


def test_unauthenticated_and_bearer_tokens_are_refused(client):
    """The long-lived bearer token went with email (D-76): a browser authenticates by device key
    or by the session cookie, and an `Authorization: Bearer` header is just unauthenticated."""
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
        push_body(lat=91.0, lon=8.0),
        push_body(lat=41.9, lon=12.5),  # Rome: outside coverage
        push_body(lat=50.0, lon=8.0, extra="field"),
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
        content=(
            f'{{"endpoint": "{endpoint()}", "p256dh": "{P256DH}", "auth": "{AUTH}", '
            '"lat": NaN, "lon": 8.0}'
        ),
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
    # The link is an <a href> now, so the token ends at the quote, not at the next tag.
    unsub = page.text.split("/unsubscribe#t=")[1].split('"')[0].strip()

    assert client.get("/unsubscribe").status_code == 200
    with db() as session:
        assert session.query(Subscriber).count() == 1  # still there

    assert client.post("/unsubscribe", data={"token": unsub}).status_code == 200
    with db() as session:
        assert session.query(Subscriber).count() == 0


# --- rate limiting ---------------------------------------------------------------------------


def test_subscribe_is_rate_limited(client, settings):
    for _ in range(settings.subscribe_limit_per_hour):
        assert subscribe(client, name="a").status_code == 202
    assert subscribe(client, name="a").status_code == 429


def test_x_forwarded_for_cannot_be_spoofed_when_no_proxy_is_trusted(client, settings):
    """trusted_proxy_hops is 0 here, so the header is ignored entirely and the limit still bites."""
    assert settings.trusted_proxy_hops == 0
    for i in range(settings.subscribe_limit_per_hour):
        assert subscribe(client, name=f"u{i}").status_code == 202
    spoofed = subscribe(client, name="u99", headers={"X-Forwarded-For": "1.2.3.4"})
    assert spoofed.status_code == 429


def test_deleting_an_account_does_not_erase_the_abuse_record(client, notifier, db):
    """Otherwise delete-and-retry resets every limit (SECURITY_REVIEW.md F-5)."""
    subscribe(client)
    client.post("/confirm", data={"token": confirm_token_from(notifier)})
    assert (
        client.delete("/api/v1/subscriptions/me", headers={CSRF_HEADER: csrf(client)}).status_code
        == 204
    )
    with db() as session:
        assert session.query(Subscriber).count() == 0
        assert session.query(RateLimitHit).count() > 0


def test_no_rate_limit_row_holds_a_client_address(db, settings, notifier):
    """Hashed with the app secret since D-73: the limits only need "same address as before"."""
    from rainalert.api.ratelimit import ip_bucket

    behind_proxy = settings.model_copy(update={"trusted_proxy_hops": 1})
    client = TestClient(create_app(behind_proxy, session_factory=db, notifier=notifier))
    response = subscribe(client, headers={"X-Forwarded-For": "203.0.113.7"})
    assert response.status_code == 202
    with db() as session:
        buckets = {row.bucket for row in session.query(RateLimitHit)}
    assert not [b for b in buckets if "203.0.113.7" in b], buckets
    assert ip_bucket("subscribe:ip", "203.0.113.7", settings.secret_key) in buckets


def test_no_bucket_is_built_from_a_plain_address():
    """The regression this guards: one new endpoint writing `f"...:{ip}"` again."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "rainalert" / "api" / "app.py").read_text()
    assert "{ip}" not in source


def test_the_purge_removes_expired_rows_and_every_plain_ip(db, settings):
    from datetime import UTC, datetime, timedelta

    from rainalert.api.ratelimit import ip_bucket, purge_old_hits

    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    hashed = ip_bucket("subscribe:ip", "203.0.113.7", settings.secret_key)
    keep = {hashed, "subscribe:webpush:" + "ab" * 32, "manage:address:" + "cd" * 32}
    legacy = {"subscribe:ip:203.0.113.7", "locate:ip:2001:db8::1", "devicekey:fail:unknown"}
    with db() as session:
        for bucket in keep | legacy:
            session.add(RateLimitHit(bucket=bucket, occurred_at=now - timedelta(minutes=5)))
        expired = ip_bucket("settings:ip", "198.51.100.1", settings.secret_key)
        session.add(RateLimitHit(bucket=expired, occurred_at=now - timedelta(days=8)))
        session.commit()

        assert purge_old_hits(session, settings.rate_limit_retention_days, now) == 4
        assert {row.bucket for row in session.query(RateLimitHit)} == keep


# --- hygiene ---------------------------------------------------------------------------------


def test_security_headers_are_present(client):
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"


def test_unconfirmed_signups_are_purged(db, settings):
    with db() as session:
        svc_subscribe(session, settings, name="ghost")
        later = datetime.now(UTC) + timedelta(hours=settings.unconfirmed_purge_hours + 1)
        assert svc.purge_unconfirmed(session, settings, now=later) == 1
        assert session.query(Subscriber).count() == 0


def test_tokens_are_never_stored_in_plaintext(db, settings, notifier):
    with db() as session:
        result = svc_subscribe(session, settings)
        stored = session.query(AuthToken).one()
        assert result.confirm_token.encode() not in bytes(stored.token_hash)
        assert len(stored.token_hash) == 32
        confirmed = svc.confirm(session, settings, token=result.confirm_token)
        hashes = {bytes(t.token_hash) for t in session.query(AuthToken).all()}
        assert confirmed.unsubscribe_token.encode() not in b"".join(hashes)
        assert all(t.purpose in set(TokenPurpose) for t in session.query(AuthToken).all())


def test_a_pasted_coordinate_is_accepted_and_reduced(client, db):
    """Seven decimals from a mapping site must not be refused - or stored.

    The form used to carry step="0.0001", so the browser rejected the value before it was ever
    sent. That put the rounding in the one place it could not be enforced, and made a legitimate
    paste look like a typo.
    """
    r = client.post("/api/v1/subscriptions", json=push_body(lat=48.153330, lon=11.557428))
    assert r.status_code in (200, 201, 202), r.text

    with db() as session:
        sub = session.execute(select(Subscription)).scalars().one()
        assert (sub.lat, sub.lon) == (48.1533, 11.5574)


def test_precision_beyond_the_policy_is_never_stored(client, db):
    """The minimisation has to hold for a caller who skips the form entirely."""
    client.post("/api/v1/subscriptions", json=push_body(lat=48.15333012345, lon=11.55742898765))
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
    # A notification body is plain text nothing linkifies, so the link is where a tap goes.
    assert "http" not in message.text
    assert (message.click_url or "").startswith("https://")
    assert "/confirm#a=" in (message.click_url or "")


def test_the_query_parameter_is_gone_rather_than_deprecated(client, db):
    """Accepting `?token=` as well would leave the logged shape working for anyone who still
    sent one - a second code path kept alive for data that only ever existed in development."""
    for path in ("/confirm", "/unsubscribe"):
        page = client.get(f"{path}?token=would-have-worked-before")
        assert page.status_code == 200
        assert "would-have-worked-before" not in page.text


def test_the_pages_say_so_when_a_link_arrives_without_its_token(client, db):
    """A bare /confirm is what a link scanner or a truncated link produces. The button must not
    sit there looking ready to work."""
    for path in ("/confirm", "/unsubscribe"):
        page = client.get(path)
        assert page.status_code == 200
        assert 'id="no-token"' in page.text
        # The fragment is read by script, so the page needs the nonce to be allowed to run.
        assert "csp_nonce" not in page.text  # rendered, not left as a literal
        assert "<script nonce=" in page.text


def test_a_scanner_cannot_spend_the_push_token_by_fetching_the_link(client, notifier, db):
    """The thing that makes confirming-on-open safe is not the marker, it is the fragment.

    A scanner fetches the URL; the fragment is never sent, so the server gets a bare `GET
    /confirm` with nothing to act on, and the page it gets back needs script *and* the fragment
    before anything happens. Asserted on the database, not on the response.
    """
    subscribe(client, name="scanner-test")
    token = confirm_token_from(notifier)

    # Everything a fetch of that URL actually puts on the wire.
    assert client.get("/confirm").status_code == 200
    with db() as session:
        assert session.query(Subscriber).one().confirmed_at is None, "a bare GET confirmed"

    # And the token still works for the person who was sent it.
    assert client.post("/confirm", data={"token": token}).status_code == 200
    with db() as session:
        assert session.query(Subscriber).one().confirmed_at is not None


def test_the_page_confirms_only_a_token_it_was_just_handed(client, db):
    """`#a=` submits as soon as the page has the token. Anything else in the fragment - a mailed
    `#t=` link from before D-76 among them - fills nothing and shows the missing-token note."""
    body = client.get("/confirm").text
    assert "hash.indexOf('#a=') === 0" in body
    assert "hash.indexOf('#t=')" not in body
    assert body.count("form.submit()") == 1
