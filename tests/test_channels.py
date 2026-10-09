"""Subscribing over the push channel (D-5 revised, D-45).

Double opt-in was never about email specifically: it is about proving the channel reaches the
person who asked. These tests are mostly about the ways that can go wrong differently for a browser
push subscription than for a mailbox.

Rewritten rather than adapted when ntfy was removed. Roughly half of what was here tested things
that no longer exist - that a generated topic was unguessable, that it was never taken from the
request, that a QR code encoded the app link, that copying it degraded without clipboard
permission. A push endpoint is issued by the browser and never shown to anyone, so none of those
questions can be asked. What replaces them is one ntfy never raised: the endpoint is a URL a
stranger picks, and we POST to it.
"""

import re
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from rainalert import subscriptions as svc
from rainalert.config import Settings
from rainalert.db.models import Channel, Subscriber, SubscriptionStatus
from rainalert.notify.base import DeliveryResult, OutboundMessage
from rainalert.notify.routing import RoutingNotifier
from rainalert.tokens import hash_address
from tests.helpers import page_source

FRANKFURT = (50.1109, 8.6821)

#: A plausible subscription, in the shape `PushSubscription.toJSON()` produces.
ENDPOINT = "https://fcm.googleapis.com/fcm/send/cVBhZ2VLZXkxMjM0NTY3ODkw"
P256DH = "BN4GvZtEZiZuqFxSKVZfSfluwlTOYPkxZswgcVYpXbPSyMkNCFYrCF2WNpgLzLTOdCcSsRPHWO4bXdhgbNJHEDo"
AUTH = "tBHItJI5svbpez7KI4CCXg"


def push_subscriber(address=ENDPOINT):
    """A stand-in with the two fields every webpush message needs."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        address=address,
        channel=Channel.WEBPUSH,
        push_p256dh=P256DH,
        push_auth=AUTH,
    )


ALERT_PAYLOAD = {
    "predicted_start_at": "2026-09-27T14:25:00+00:00",
    "cycle_time": "2026-09-27T14:00:00+00:00",
    "lead_minutes": 25,
    "peak_mm_5min": 0.4,
    "timezone": "Europe/Berlin",
}


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
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    return TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))


def subscribe_body(**overrides):
    body = {
        "channel": "webpush",
        "lat": FRANKFURT[0],
        "lon": FRANKFURT[1],
        "endpoint": ENDPOINT,
        "p256dh": P256DH,
        "auth": AUTH,
    }
    body.update(overrides)
    return body


def alert_for(channel, address):
    from rainalert.api.mail import alert_message

    # The keys are part of a push subscriber, not decoration: `OutboundMessage` refuses a webpush
    # message without them, which is what makes a builder that forgets them fail in the suite.
    subscriber = SimpleNamespace(
        id=uuid.uuid4(),
        address=address,
        channel=channel,
        push_p256dh=P256DH if channel is Channel.WEBPUSH else None,
        push_auth=AUTH if channel is Channel.WEBPUSH else None,
    )
    return alert_message(
        None,
        _SETTINGS_FOR_ALERT,
        subscriber,
        SimpleNamespace(timezone="Europe/Berlin"),
        ALERT_PAYLOAD,
    )


_SETTINGS_FOR_ALERT = Settings(
    database_url="postgresql+psycopg://unused",
    public_base_url="https://rain.example.invalid",
    secret_key="test-secret",
    notifier="console",
    _env_file=None,
)


# --- the endpoint is the identity, and it is attacker-supplied --------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/x",  # not https
        "https://169.254.169.254/computeMetadata/v1/instance/",  # the metadata service
        "https://10.0.0.5/internal",
        "https://localhost:5432/",
        "https://fcm.googleapis.com.evil.test/x",  # an allowed host as a prefix of another
        "https://notfcm.googleapis.com/x",  # an allowed host as a suffix
    ],
)
def test_an_endpoint_that_is_not_a_push_service_is_refused(client, db, endpoint):
    """The endpoint is a URL chosen by whoever is calling, and the notifier POSTs to it.

    Without the host check this signup form is a server-side request forgery primitive: it makes
    this service fetch a URL of the caller's choosing from inside its own network, and hands the
    result back through a notification error. Refused before anything is stored.
    """
    response = client.post("/api/v1/subscriptions", json=subscribe_body(endpoint=endpoint))
    assert response.status_code == 422, endpoint
    with db() as session:
        assert session.execute(select(Subscriber)).first() is None, f"{endpoint} was stored"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://web.push.apple.com/abc",
    ],
)
def test_the_real_push_services_are_accepted(client, db, endpoint):
    response = client.post("/api/v1/subscriptions", json=subscribe_body(endpoint=endpoint))
    assert response.status_code == 202


def test_the_same_browser_subscribing_twice_is_one_subscriber(client, db):
    """Re-subscribing is normal: `pushManager.subscribe()` returns the same endpoint, and the page
    calls it on every signup. It must not mint a second row."""
    for _ in range(2):
        assert client.post("/api/v1/subscriptions", json=subscribe_body()).status_code == 202
    with db() as session:
        assert len(session.execute(select(Subscriber)).scalars().all()) == 1


def test_resubscribing_never_overwrites_the_stored_keys(db, settings):
    """The opposite of what this used to assert, and the reversal is the point.

    It read "resubscribing refreshes the keys", on the reasoning that a browser handing back the same
    endpoint with new keys should be believed. It should not: an endpoint is not a secret - a push
    service will not deliver to it for anyone without our VAPID key - so anyone who learns one could
    replace the keys of a confirmed subscriber and silence them invisibly, the push service still
    answering 201 while their browser fails to decrypt. That is the hole `/push/resubscribe` was
    deleted for, and it was briefly reopened here.

    Nothing legitimate needs the overwrite: `pushManager.subscribe()` with the same
    applicationServerKey returns the existing subscription, so the same browser presents the same
    pair. A genuinely rotated key comes with a new endpoint, hence a new row.
    """
    with db() as session:
        svc.subscribe(
            session,
            settings,
            channel=Channel.WEBPUSH,
            address=ENDPOINT,
            push_p256dh=P256DH,
            push_auth=AUTH,
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        subscriber = session.execute(select(Subscriber)).scalar_one()
        subscriber.confirmed_at = subscriber.created_at
        session.commit()

        svc.subscribe(
            session,
            settings,
            channel=Channel.WEBPUSH,
            address=ENDPOINT,
            push_p256dh="ATTACKER" + P256DH[8:],
            push_auth="ATTACKERattackerattac1",
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        session.expire_all()
        subscriber = session.execute(select(Subscriber)).scalar_one()
        assert subscriber.push_p256dh == P256DH
        assert subscriber.push_auth == AUTH


def test_an_endpoint_longer_than_a_mailbox_is_stored_whole(db, settings):
    """`address` was VARCHAR(320), the RFC 5321 bound on a mailbox. A push endpoint has no such
    bound, and a truncated one is a subscriber who can never be reached and cannot be repaired."""
    long_endpoint = "https://fcm.googleapis.com/fcm/send/" + ("x" * 400)
    with db() as session:
        svc.subscribe(
            session,
            settings,
            channel=Channel.WEBPUSH,
            address=long_endpoint,
            push_p256dh=P256DH,
            push_auth=AUTH,
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        assert session.execute(select(Subscriber)).scalar_one().address == long_endpoint


# --- what the two channels will and will not accept -------------------------------------------


def test_an_email_address_on_the_push_channel_is_refused_not_ignored(client, db):
    """Silently dropping an address someone supplied is how they end up believing it was stored."""
    response = client.post("/api/v1/subscriptions", json=subscribe_body(email="a@b.example"))
    assert response.status_code == 422
    with db() as session:
        assert session.execute(select(Subscriber)).first() is None


def test_a_push_subscription_on_the_email_channel_is_refused(client, db):
    body = subscribe_body(channel="email", email="a@b.example")
    assert client.post("/api/v1/subscriptions", json=body).status_code == 422


@pytest.mark.parametrize("missing", ["endpoint", "p256dh", "auth"])
def test_two_of_the_three_push_values_is_refused(client, db, missing):
    """All three or none. A row with an endpoint and no keys can be stored and never delivered to,
    which looks like a working subscription from both ends."""
    body = subscribe_body()
    body.pop(missing)
    assert client.post("/api/v1/subscriptions", json=body).status_code == 422
    with db() as session:
        assert session.execute(select(Subscriber)).first() is None


def test_email_still_requires_an_address(client, db):
    response = client.post(
        "/api/v1/subscriptions",
        json={"channel": "email", "lat": FRANKFURT[0], "lon": FRANKFURT[1]},
    )
    assert response.status_code == 422


def test_an_endpoint_spelled_like_a_mailbox_is_a_different_subscriber():
    """The hash covers the channel, so the two namespaces cannot collide."""
    assert hash_address("email", "a@b.example") != hash_address("webpush", "a@b.example")


def test_endpoints_are_not_case_folded(db, settings):
    """A mailbox folds case; a URL path does not. Folding an endpoint would change which
    subscription it names."""
    mixed = "https://fcm.googleapis.com/fcm/send/AbCdEf"
    with db() as session:
        svc.subscribe(
            session,
            settings,
            channel=Channel.WEBPUSH,
            address=mixed,
            push_p256dh=P256DH,
            push_auth=AUTH,
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        assert session.execute(select(Subscriber)).scalar_one().address == mixed


# --- double opt-in ----------------------------------------------------------------------------


def test_a_push_subscription_is_pending_until_the_notification_is_tapped(db, settings):
    """Confirmation keeps its place on web push, for the reason it always had: it proves the channel
    reaches the subscriber. There is more of that chain to get wrong here, not less - a service
    worker that fails to install, a permission revoked between subscribing and the first send."""
    with db() as session:
        result = svc.subscribe(
            session,
            settings,
            channel=Channel.WEBPUSH,
            address=ENDPOINT,
            push_p256dh=P256DH,
            push_auth=AUTH,
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        subscriber = session.get(Subscriber, result.subscriber_id)
        assert subscriber.confirmed_at is None
        assert subscriber.subscriptions[0].status is SubscriptionStatus.PENDING
        assert result.confirm_token


def test_the_confirmation_is_tappable_and_prints_no_url(settings):
    """A notification body is plain text that nothing linkifies, so a URL printed in one is an exit
    the reader can see and not take. Tapping it is the way through, which is `click_url`."""
    from rainalert.api.mail import confirmation_message

    message = confirmation_message(
        settings, ENDPOINT, "tok123", channel="webpush", subscriber=push_subscriber()
    )
    assert message.channel == "webpush"
    # `#a=` is confirm-on-open. The extra click mail keeps exists to defend against mail scanners
    # (F-4), and none of those sits between us and a phone.
    assert message.click_url.endswith("/confirm#a=tok123")
    assert "http" not in message.text, message.text


def test_the_email_confirmation_still_waits_for_a_click(settings):
    from rainalert.api.mail import confirmation_message

    message = confirmation_message(settings, "a@b.example", "tok123", channel="email")
    assert "#t=tok123" in message.click_url
    assert "/confirm#t=tok123" in message.text


# --- the way out ------------------------------------------------------------------------------


def test_a_push_alert_carries_no_buttons_and_no_unsubscribe_url():
    """The exit on push is the settings page, which carries "Abmelden und meine Daten loeschen" and
    is reached through the site. Notifications carry no buttons at all (D-64); tapping a warning
    opens the map at the warned place."""
    message = alert_for(Channel.WEBPUSH, ENDPOINT)
    assert "#l=" in message.click_url
    assert "Abmelden:" not in message.text
    assert "http" not in message.text


def test_an_email_alert_still_carries_the_unsubscribe_line():
    message = alert_for(Channel.EMAIL, "a@b.example")
    assert "Abmelden: https://" in message.text
    assert "List-Unsubscribe" in message.headers


# --- routing ----------------------------------------------------------------------------------


class Spy:
    def __init__(self):
        self.seen = []

    def send(self, message):
        self.seen.append(message)
        return DeliveryResult(ok=True)


def test_a_mailbox_is_never_handed_to_the_push_transport():
    """The failure this exists to prevent, in its original form: with one notifier for everything,
    an email subscriber's confirmation was published to a public ntfy topic named after their
    address. The transports changed; a message reaching the wrong one still means somebody's
    warning was not delivered."""
    email, push = Spy(), Spy()
    router = RoutingNotifier(email=email, webpush=push)
    router.send(OutboundMessage(to="a@b.example", subject="s", text="t", channel="email"))
    router.send(
        OutboundMessage(
            to=ENDPOINT,
            subject="s",
            text="t",
            channel="webpush",
            push_p256dh=P256DH,
            push_auth=AUTH,
        )
    )
    assert [m.to for m in email.seen] == ["a@b.example"]
    assert [m.to for m in push.seen] == [ENDPOINT]


def test_a_channel_with_no_transport_raises_rather_than_falling_back():
    """A fallback here would be the same bug wearing a helpful face."""
    router = RoutingNotifier(email=Spy())
    with pytest.raises(ValueError, match="no transport configured"):
        router.send(
            OutboundMessage(
                to=ENDPOINT,
                subject="s",
                text="t",
                channel="webpush",
                push_p256dh=P256DH,
                push_auth=AUTH,
            )
        )


def test_the_production_notifier_routes_and_the_dev_ones_do_not(settings):
    """`console` and `file` stay sinks that take everything, so a local run never posts to a real
    push service because a test subscriber happened to pick push."""
    from rainalert.notify import build_notifier
    from rainalert.notify.webpush import generate_vapid_keys

    assert not isinstance(build_notifier("console", settings), RoutingNotifier)
    assert not isinstance(build_notifier("file", settings), RoutingNotifier)
    private, _ = generate_vapid_keys()
    # Both, because `auto` builds a real `WebPushNotifier` and that refuses to exist without a
    # subject - `vapid_subject` defaults to empty rather than to a placeholder, so a deployment
    # that forgets it fails at startup instead of signing every JWT with `ops@example.invalid`
    # and being rejected by the push service. Terraform always passes one (falling back to
    # `mailto:${alert_email}`), so the empty state is local-only.
    production = settings.model_copy(
        update={
            "vapid_private_key": private,
            "vapid_subject": "mailto:ops@rain.example.invalid",
        }
    )
    assert isinstance(build_notifier("auto", production), RoutingNotifier)


def test_auto_disables_push_rather_than_refusing_to_start(settings):
    """A misconfigured push channel must not take the rest of the service down with it.

    This test previously asserted the opposite - that `auto` raises without a usable VAPID subject,
    so a deployment which forgot one failed loudly at startup. Review showed what "loudly at startup"
    means on Cloud Run: `create_app` builds the notifier before anything else, so an empty, disabled
    or unreadable VAPID secret made the process raise at import, no revision ever became ready, and
    the map, the radar, the privacy page and the *email* channel went down with the push channel.
    `api/app.py` already had a comment saying that must not happen, one screen below the code that
    made it happen.

    So the contract is now: build what can be built, log the rest, and let `RoutingNotifier` refuse
    per *message*. A push subscriber's warning fails and is recorded as failed; nobody else notices.
    """
    from rainalert.notify import RoutingNotifier, build_notifier
    from rainalert.notify.base import OutboundMessage
    from rainalert.notify.webpush import generate_vapid_keys

    private, _ = generate_vapid_keys()
    for label, update in [
        ("no subject", {"vapid_private_key": private, "vapid_subject": ""}),
        ("no key", {"vapid_private_key": "", "vapid_subject": "mailto:ops@rain.example.invalid"}),
        ("malformed key", {"vapid_private_key": "nonsense", "vapid_subject": "mailto:a@b.invalid"}),
    ]:
        notifier = build_notifier("auto", settings.model_copy(update=update))
        assert isinstance(notifier, RoutingNotifier), label
        # Email still works - that is the whole point.
        assert "email" in notifier._by_channel, label
        # And a push message fails as one message, not as the process.
        with pytest.raises(ValueError, match="no transport configured"):
            notifier.send(
                OutboundMessage(
                    to="https://fcm.googleapis.com/fcm/send/x",
                    channel="webpush",
                    subject="s",
                    text="t",
                    push_p256dh="A" * 87,
                    push_auth="B" * 22,
                )
            )


def test_every_message_the_service_builds_names_its_channel(settings):
    """Nothing may be sent without a channel: that field is what keeps an address off the wrong
    transport, and a default of "email" on a push message would be exactly that bug."""
    from rainalert.api.mail import (
        confirmation_message,
        deletion_receipt,
        manage_link_message,
        push_keys,
    )

    who = push_subscriber()
    for message in (
        confirmation_message(settings, ENDPOINT, "t", channel="webpush", subscriber=who),
        manage_link_message(settings, ENDPOINT, "t", who.id, channel="webpush", subscriber=who),
        deletion_receipt(settings, ENDPOINT, channel="webpush", push=push_keys(who)),
    ):
        assert message.channel == "webpush", message.subject
    for message in (
        confirmation_message(settings, "a@b.example", "t"),
        manage_link_message(settings, "a@b.example", "t", uuid.uuid4()),
        deletion_receipt(settings, "a@b.example"),
    ):
        assert message.channel == "email", message.subject


# --- rate limiting ----------------------------------------------------------------------------


def test_the_limiter_answers_429_rather_than_a_validation_error(client, db, settings):
    """429 and not 422: the input was fine, and the distinction is what the page needs to tell
    somebody they have run out of attempts rather than sending them round the form again."""
    last = None
    for index in range(settings.subscribe_limit_per_hour + 2):
        last = client.post(
            "/api/v1/subscriptions", json=subscribe_body(endpoint=f"{ENDPOINT}{index}")
        )
    assert last.status_code == 429


def strip_js_comments(source: str) -> str:
    """Remove `//` and `/* */` comments so an assertion can be about the copy rather than the prose
    explaining it.

    Deliberately crude - it does not parse strings, so a `//` inside a string literal would be cut.
    That is fine here: these tests match German sentences, and no German sentence in this codebase
    contains `//`. A real tokeniser would be more correct and would earn nothing.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_the_page_does_not_blame_a_rate_limited_signup_on_the_input(client):
    """The counterpart to the test above, where the wording actually lives. One message for every
    failure told people to check their input even when the limiter had simply run out."""
    # Comments stripped first. This assertion is about the copy a reader sees, and twice now it has
    # failed because an explanatory comment either contained the forbidden phrase or grew long
    # enough to push the real text out of a fixed-size window. A comment is not user-facing copy, so
    # it has no business being matched against.
    body = strip_js_comments(page_source(client))
    assert "response.status === 429" in body
    limiter_branch = body.split("response.status === 429")[1][:400]
    # Matched on the claim, not the exact phrasing: this failed once for "an den" becoming
    # "an deinen", which is a copy edit rather than the regression it exists to catch.
    assert "Eingaben liegt es nicht" in limiter_branch
    # And it must not name a cause it cannot know: subscribe limits per IP *and* per address, so
    # blaming the connection is wrong whenever the address half is what tripped.
    assert "dieser Verbindung" not in limiter_branch


def test_the_endpoint_is_rate_limited_as_well_as_the_ip(client, db, settings):
    """A browser re-subscribing is normal; a flood naming one endpoint is not, and the endpoint is
    the only per-subscriber thing such a flood would have in common."""
    seen = set()
    for _ in range(settings.subscribe_limit_per_hour + 2):
        seen.add(client.post("/api/v1/subscriptions", json=subscribe_body()).status_code)
    assert 429 in seen


# --- push-only deployments --------------------------------------------------------------------


@pytest.fixture()
def push_only_client(db, settings):
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    push_only = settings.model_copy(update={"email_channel_enabled": False})
    return TestClient(create_app(push_only, session_factory=db, notifier=ConsoleNotifier()))


def test_the_email_channel_can_be_refused_before_anything_is_stored(push_only_client, db):
    response = push_only_client.post(
        "/api/v1/subscriptions",
        json={
            "channel": "email",
            "email": "a@b.example",
            "lat": FRANKFURT[0],
            "lon": FRANKFURT[1],
        },
    )
    assert response.status_code == 422
    with db() as session:
        assert session.execute(select(Subscriber)).first() is None


def test_push_still_works_when_email_is_off(push_only_client, db):
    response = push_only_client.post("/api/v1/subscriptions", json=subscribe_body())
    assert response.status_code == 202


def test_the_page_stops_offering_a_choice_it_would_reject(push_only_client):
    # The rendered page, not `page_source`: both assertions are about markup the template emits,
    # so pulling the scripts in would only widen what could accidentally satisfy them.
    body = push_only_client.get("/").text
    assert 'value="webpush"' in body
    # The fieldset is hidden rather than dropped: the push radio stays checked and in the DOM,
    # which is what the script reads.
    assert 'class="channel" hidden' in body


def test_push_key_bounds_match_the_columns():
    """The request model must not accept a key the column cannot hold.

    They disagreed: `p256dh` was validated at 256 characters into a `String(128)`, `auth` at 128
    into a `String(64)`. Anything in the gap passed validation and blew up on the flush as a
    `DataError` - a 500 out of a public, unauthenticated endpoint, reachable by anyone willing to
    post a long string. Equal is the only safe relation: looser is a validator handing the database
    input it cannot store, and tighter would refuse a key we could have kept.
    """
    from rainalert.api.app import SubscribeRequest
    from rainalert.db.models import Subscriber

    columns = Subscriber.__table__.c
    fields = SubscribeRequest.model_fields

    def max_length(name):
        return next(m.max_length for m in fields[name].metadata if hasattr(m, "max_length"))

    assert max_length("p256dh") == columns.push_p256dh.type.length
    assert max_length("auth") == columns.push_auth.type.length


@pytest.mark.parametrize(("field", "column"), [("p256dh", "push_p256dh"), ("auth", "push_auth")])
def test_a_key_too_long_for_the_column_is_refused_not_a_server_error(client, field, column):
    """422, not 500, and the length comes from the *column* on purpose.

    Deriving it from the request model instead would make this test circular: it would post one
    character more than whatever the validator happens to allow and pass however wide that is,
    including the 256 that caused the bug. Measured against the column, it fails the way a user
    would find it - one character past what the database can store is an unhandled `DataError`
    inside the transaction, which is a 500 from a public endpoint. Verified against the old bounds:
    with `max_length=256` this posts 129 characters and gets a 500.
    """
    from rainalert.db.models import Subscriber

    width = Subscriber.__table__.c[column].type.length
    response = client.post(
        "/api/v1/subscriptions", json=subscribe_body(**{field: "A" * (width + 1)})
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(("field", "column"), [("p256dh", "push_p256dh"), ("auth", "push_auth")])
def test_a_key_that_exactly_fills_the_column_is_still_accepted(client, field, column):
    """The counterpart: the bound is where storage stops, not a guess at what a browser sends, so
    a key that exactly fills the column must go through rather than be refused one character
    early."""
    from rainalert.db.models import Subscriber

    width = Subscriber.__table__.c[column].type.length
    response = client.post("/api/v1/subscriptions", json=subscribe_body(**{field: "A" * width}))
    assert response.status_code == 202, response.text


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com:0x1bb/x",
        "https://[fcm.googleapis.com]/x",
        "https://℀.fcm.googleapis.com/x",
    ],
)
def test_an_endpoint_that_breaks_the_parser_is_a_422_from_the_route(client, endpoint):
    """The route half of the same contract. `check_endpoint` raising the right type is only useful
    if nothing between it and the client converts that into a 500, so this asserts the status a
    stranger actually sees. It is a separate test from the unit one because the unit one passed
    while the route still returned 500: `subscribe` catches `EndpointRefused` and the route catches
    `ValidationError`, and a bare `ValueError` fell between them."""
    response = client.post("/api/v1/subscriptions", json=subscribe_body(endpoint=endpoint))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("field", ["p256dh", "auth"])
def test_a_push_key_outside_base64url_is_refused(client, field):
    """A browser's keys are base64url. Anything else cannot be one under any encoding, so 422 is
    the honest answer - and it keeps non-ASCII away from the constant-time comparison in
    `subscriptions.subscribe`, which used to raise TypeError on it and 500."""
    response = client.post("/api/v1/subscriptions", json=subscribe_body(**{field: "ä" * 22}))
    assert response.status_code == 422, response.text


def test_a_non_ascii_authorization_header_is_401_not_500(db, settings):
    """Headers are bytes on the wire and Starlette decodes them as latin-1, so an `Authorization`
    value can hold non-ASCII characters that `compare_digest` refused to compare. The answer to a
    bad token is 401 whatever bytes it contains."""
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    guarded = settings.model_copy(update={"metrics_token": "sekret"})
    client = TestClient(
        create_app(guarded, session_factory=db, notifier=ConsoleNotifier()),
        raise_server_exceptions=False,
    )
    # Passed as bytes: httpx will not encode a non-ASCII str into a header, but a real client can
    # put these bytes on the wire, which is the case that mattered.
    assert (
        client.get("/metrics", headers={"Authorization": b"Bearer \xe4\xe4\xe4"}).status_code == 401
    )
    assert client.get("/metrics", headers={"Authorization": b"Bearer nope"}).status_code == 401


def test_the_two_address_bounds_agree():
    """A subscriber who can sign up must be able to reach their own settings.

    These were 2048 and unbounded, in that order, so a browser issuing a longer endpoint subscribed
    successfully and then got a 422 from `/api/v1/manage/link` forever. Fixing that by removing both
    bounds left an unauthenticated body with no ceiling, which was the wrong lesson to draw.
    """
    from rainalert.api.app import ManageLinkRequest, SubscribeRequest

    def cap(model, field):
        return next(
            m.max_length for m in model.model_fields[field].metadata if hasattr(m, "max_length")
        )

    assert cap(SubscribeRequest, "endpoint") == cap(ManageLinkRequest, "address")
