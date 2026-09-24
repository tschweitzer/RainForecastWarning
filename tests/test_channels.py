"""Subscribing over a push channel (D-5 revised).

Double opt-in was never about email specifically: it is about proving the channel reaches the
person who asked. These tests are mostly about the ways that can go wrong differently for a push
topic than for a mailbox.
"""

import uuid

import httpx
import pytest
from sqlalchemy import select

from rainalert import subscriptions as svc
from rainalert.config import Settings
from rainalert.db.models import Channel, Subscriber, SubscriptionStatus
from rainalert.notify.base import DeliveryResult, OutboundMessage
from rainalert.notify.ntfy import NtfyNotifier
from rainalert.notify.routing import RoutingNotifier
from rainalert.tokens import hash_address
from tests.helpers import Recorder

FRANKFURT = (50.1109, 8.6821)


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


# --- the topic ------------------------------------------------------------------------------


def test_topics_are_unguessable_and_never_repeat():
    """A public ntfy topic is a flat namespace: whoever knows it can subscribe and read.

    A rain warning says where and when it will rain for the person receiving it, so a guessable
    topic is a location leak. 128 bits is the whole defence.
    """
    topics = {svc.new_ntfy_topic() for _ in range(500)}
    assert len(topics) == 500

    random_part = next(iter(topics)).removeprefix("rainalert-")
    # url-safe base64 of 16 bytes: 22 characters, ~128 bits.
    assert len(random_part) >= 22


def test_the_topic_is_generated_not_taken_from_the_request(db, settings):
    """Anything a caller supplies could be chosen to collide with somebody else's topic."""
    with db() as session:
        result = svc.subscribe(
            session,
            settings,
            channel=Channel.NTFY,
            address="rainalert-i-picked-this",
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        assert result.address != "rainalert-i-picked-this"
        assert result.address.startswith("rainalert-")


def test_an_email_address_on_the_ntfy_channel_is_refused_not_ignored(client, db):
    """Silently dropping it is how someone ends up believing it was stored."""
    response = client.post(
        "/api/v1/subscriptions",
        json={"channel": "ntfy", "email": "me@example.com", "lat": 50.11, "lon": 8.68},
    )
    assert response.status_code == 422


def test_the_ntfy_channel_needs_no_address(client, db):
    response = client.post(
        "/api/v1/subscriptions", json={"channel": "ntfy", "lat": 50.11, "lon": 8.68}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["topic"].startswith("rainalert-")
    assert body["subscribe_url"].endswith(body["topic"])


def test_email_still_requires_an_address(client, db):
    response = client.post(
        "/api/v1/subscriptions", json={"channel": "email", "lat": 50.11, "lon": 8.68}
    )
    assert response.status_code == 422


# --- identity -------------------------------------------------------------------------------


def test_a_topic_spelled_like_a_mailbox_is_a_different_subscriber(db):
    """The hash covers the channel, or these two would be one row."""
    assert hash_address("email", "a@example.com") != hash_address("ntfy", "a@example.com")


def test_mailboxes_fold_case_and_topics_do_not():
    """ntfy treats Abc and abc as different topics; folding them would merge two destinations."""
    assert hash_address("email", "A@Example.COM") == hash_address("email", "a@example.com")
    assert hash_address("ntfy", "Rain-ABC") != hash_address("ntfy", "rain-abc")


# --- confirmation ---------------------------------------------------------------------------


def test_a_push_subscription_is_pending_until_the_notification_is_tapped(db, settings):
    """The reason differs from email - there is no third party to protect - but the property
    does not: nothing is warned about until the channel has been shown to work."""
    with db() as session:
        result = svc.subscribe(
            session, settings, channel=Channel.NTFY, lat=FRANKFURT[0], lon=FRANKFURT[1]
        )
        subscriber = session.execute(
            select(Subscriber).where(
                Subscriber.address_hash == hash_address("ntfy", result.address)
            )
        ).scalar_one()
        assert subscriber.confirmed_at is None
        assert subscriber.subscriptions[0].status is SubscriptionStatus.PENDING

        confirmed = svc.confirm(session, settings, token=result.confirm_token)
        assert confirmed is not None
        session.refresh(subscriber)
        assert subscriber.confirmed_at is not None


def test_the_confirmation_carries_a_tappable_link(db):
    """A push notification has nowhere to put a link except the click action.

    A confirmation that works in a mail client and does nothing on a phone is the failure this
    exists to prevent.
    """
    from rainalert.api.mail import confirmation_message

    settings = Settings(
        database_url="postgresql+psycopg://x",
        public_base_url="https://rain.example",
        _env_file=None,
    )
    message = confirmation_message(settings, "rainalert-abc", "tok123", channel="ntfy")
    assert message.click_url == "https://rain.example/confirm#a=tok123"  # push: D-36
    assert "tok123" in message.text  # and in the body, for anyone reading it as text


# --- the notifier ---------------------------------------------------------------------------


def _notifier(recorder: Recorder, **kwargs) -> NtfyNotifier:
    return NtfyNotifier(server="https://ntfy.example", transport=recorder.transport(), **kwargs)


def test_the_topic_goes_in_the_path_and_the_title_in_a_header():
    rec = Recorder(httpx.Response(200, headers={"X-Message-Id": "abc"}))
    result = _notifier(rec).send(
        OutboundMessage(to="rainalert-xyz", subject="Regen", text="gleich", click_url="https://x/c")
    )
    assert result.ok and result.provider_message_id == "abc"

    request = rec.requests[0]
    assert str(request.url) == "https://ntfy.example/rainalert-xyz"
    assert request.headers["Title"] == "Regen"
    assert request.headers["Click"] == "https://x/c"
    assert request.content == b"gleich"


def test_a_refusal_from_ntfy_is_reported_not_swallowed():
    rec = Recorder(httpx.Response(429, text="too many requests"))
    result = _notifier(rec).send(OutboundMessage(to="t", subject="s", text="x"))
    assert not result.ok
    assert "429" in result.error and "too many" in result.error


def test_a_bearer_token_is_sent_when_configured():
    """For a self-hosted server with access control, which is the answer to ntfy.sh seeing the
    message text."""
    rec = Recorder(httpx.Response(200))
    _notifier(rec, token="secret").send(OutboundMessage(to="t", subject="s", text="x"))
    assert rec.requests[0].headers["Authorization"] == "Bearer secret"


def test_the_unsubscribe_link_reaches_the_body():
    """There is no List-Unsubscribe header on a push, so it has to be in what the reader sees."""
    rec = Recorder(httpx.Response(200))
    _notifier(rec).send(
        OutboundMessage(
            to="t", subject="s", text="Regen", headers={"List-Unsubscribe": "<https://x/u?t=1>"}
        )
    )
    assert b"https://x/u?t=1" in rec.requests[0].content


def test_a_line_break_in_a_field_is_still_refused():
    """The topic ends up in a URL and the subject in a header."""
    with pytest.raises(ValueError):
        OutboundMessage(to="topic\nX-Evil: 1", subject="s", text="x")
    with pytest.raises(ValueError):
        OutboundMessage(to="t", subject="s", text="x", click_url="https://x\nY: 2")


def test_the_whole_push_flow_end_to_end(db, settings):
    """Subscribe, receive the test push, tap it, become active.

    The one test that would have caught a confirmation link that never reaches a phone.
    """

    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app

    rec = Recorder(*[httpx.Response(200) for _ in range(4)])
    push = NtfyNotifier(server="https://ntfy.example", transport=rec.transport())
    client = TestClient(create_app(settings, session_factory=db, notifier=push))

    created = client.post(
        "/api/v1/subscriptions", json={"channel": "ntfy", "lat": 50.11, "lon": 8.68}
    )
    assert created.status_code == 202
    topic = created.json()["topic"]

    # the test push went to that topic, and nowhere else
    assert str(rec.requests[0].url).endswith(f"/{topic}")

    # tapping it means opening the Click URL, which is a GET; confirming is the POST behind it
    link = rec.requests[0].headers["Click"]
    token = link.split("/confirm#a=")[1]
    assert client.get("/confirm").status_code == 200  # the page, changes nothing
    assert client.post("/confirm", data={"token": token}).status_code == 200

    with db() as session:
        subscriber = session.execute(
            select(Subscriber).where(Subscriber.address_hash == hash_address("ntfy", topic))
        ).scalar_one()
        assert subscriber.confirmed_at is not None
        assert subscriber.subscriptions[0].status is SubscriptionStatus.ACTIVE


def test_the_topic_never_travels_in_a_url(client, db):
    """The QR used to be `<img src="/qr?text=https://ntfy.sh/<topic>">`.

    A topic is not a hint, it is the credential: whoever has one can subscribe to it, ask for a
    settings link on it and read the location. tokens.py states the rule that shape was breaking
    - "a token must never travel in a URL that ends up in a log" - and uvicorn and Cloud Run both
    log the query string. So the QR comes back in the response body and the endpoint is gone,
    which also retires the allow-list that kept it from encoding somebody else's URL.
    """
    response = client.post(
        "/api/v1/subscriptions", json={"channel": "ntfy", "lat": 50.11, "lon": 8.68}
    )
    body = response.json()
    assert body["qr_svg"].lstrip().startswith("<svg")

    assert client.get("/qr", params={"text": body["subscribe_url"]}).status_code == 404
    assert "?text=" not in client.get("/").text


def test_the_phone_can_subscribe_without_scanning_its_own_screen(client):
    """Signing up on the phone you want warned is the normal case, and the QR is useless there.

    It used to be folded away behind a disclosure on every platform, which was the compromise
    available while all three got the same markup. Now the phone branch does not render one at
    all, and the desktop branch shows it open - so this holds more strongly than it did.
    """
    body = client.get("/").text
    mobile = body.split("if (here === 'desktop')")[1].split("} else {")[1]
    assert "qrCode()" not in mobile

    # Typing the topic in by hand is still there for whatever the app did not register as a
    # link handler - behind a disclosure, because it is the path nobody should need.
    assert "navigator.clipboard.writeText" in body
    assert "Kopieren" in body
    assert "Thema von Hand eintragen" in body


def test_copying_still_offers_something_without_clipboard_permission(client):
    """Clipboard access needs a secure context and permission, and neither is guaranteed."""
    body = client.get("/").text
    assert "selectNodeContents" in body


def test_a_rate_limited_signup_is_not_blamed_on_the_input(client, db):
    """The page said "check your input" for every failure, including the limiter.

    That is the one case where checking the input changes nothing, and following the advice
    spends the attempts the person did not know they were short of.
    """
    body = client.get("/").text
    assert "response.status === 429" in body
    assert "an den Eingaben liegt es nicht" in body

    # and the server really does answer 429 rather than something vaguer
    for _ in range(6):
        last = client.post(
            "/api/v1/subscriptions", json={"channel": "ntfy", "lat": 50.11, "lon": 8.68}
        )
    assert last.status_code == 429


def test_a_push_body_never_carries_the_one_click_url():
    """The notifier appends an unsubscribe line when the body has none, taken from the
    `List-Unsubscribe` header. D-33 dropped one-click, so that header is the fragment link now -
    the query-string shape below is the one it used to carry, kept here as the thing that must
    never reach a push body if it ever comes back.

    Checked on what is actually published rather than on the OutboundMessage, because the
    appending happens inside `send`: a test that reads `message.text` sees the body before the
    notifier has touched it, and would pass with the leak reinstated.
    """
    rec = Recorder()
    _notifier(rec).send(
        OutboundMessage(
            to="rainalert-abc",
            subject="Regen in etwa 20 Minuten",
            text="Es faengt bald an zu regnen.\n\nAbmelden: https://rain.example/unsubscribe#t=tok",
            headers={"List-Unsubscribe": "<https://rain.example/unsubscribe?token=tok>"},
        )
    )
    published = rec.requests[0].content.decode("utf-8")
    assert "/unsubscribe#t=tok" in published
    assert "?token=" not in published, "the logged one-click shape reached a push body"


def test_a_push_still_gets_an_unsubscribe_line_when_the_body_has_none():
    """The append is not dead code - it is what stops a push arriving with no way out."""
    rec = Recorder()
    _notifier(rec).send(
        OutboundMessage(
            to="rainalert-abc",
            subject="Regen",
            text="Es faengt bald an zu regnen.",
            headers={"List-Unsubscribe": "<https://rain.example/unsubscribe?token=tok>"},
        )
    )
    assert (
        "Abmelden: https://rain.example/unsubscribe?token=tok" in rec.requests[0].content.decode()
    )


# --- one process, two transports ---------------------------------------------------------


class Spy:
    """A notifier that records instead of delivering."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> DeliveryResult:
        self.sent.append(message)
        return DeliveryResult(ok=True)


def test_a_mailbox_is_never_published_to_the_push_server():
    """The bug this exists to prevent, stated as the test.

    There used to be one notifier per process, picked by NOTIFIER and applied to everything, and
    `NtfyNotifier.send` puts `message.to` in the URL path. So with NOTIFIER=ntfy an email
    subscriber's confirmation was POSTed to `<server>/<their address>`: the address becomes a
    public topic name and the confirmation link becomes that topic's contents.
    """
    mail, push = Spy("mail"), Spy("push")
    router = RoutingNotifier(email=mail, ntfy=push)

    router.send(OutboundMessage(to="someone@example.org", channel="email", subject="s", text="t"))
    router.send(OutboundMessage(to="rainalert-abc", channel="ntfy", subject="s", text="t"))

    assert [m.to for m in push.sent] == ["rainalert-abc"]
    assert [m.to for m in mail.sent] == ["someone@example.org"]


def test_a_channel_with_no_transport_raises_rather_than_falling_back():
    """A fallback would be the same bug wearing a helpful face: the message still leaves, and
    still on the wrong transport. Refusing to send is the outcome we want from a mismatch."""
    router = RoutingNotifier(ntfy=Spy("push"))
    with pytest.raises(ValueError, match="email"):
        router.send(
            OutboundMessage(to="someone@example.org", channel="email", subject="s", text="t")
        )


def test_the_production_notifier_routes_and_the_dev_ones_do_not(settings):
    """`console` and `file` are sinks - a local run must not start publishing to a public ntfy
    server because a test subscriber picked push."""
    from rainalert.notify import ConsoleNotifier, build_notifier

    assert isinstance(build_notifier("auto", settings), RoutingNotifier)
    assert isinstance(build_notifier("console", settings), ConsoleNotifier)


def test_every_message_the_service_builds_carries_its_channel(db, settings):
    """Routing is only as good as the label, and the label is set at five separate call sites.

    An unlabelled message defaults to email, so a push message that forgot to say so would be
    handed to SMTP - which fails, loudly, but only in production and only for that subscriber.
    """
    from types import SimpleNamespace

    from rainalert.api.mail import (
        alert_message,
        confirmation_message,
        deletion_receipt,
        manage_link_message,
        settings_anchor_message,
    )

    topic = "rainalert-abc"
    # The warning itself, first: it is the message the whole service exists to send, the only
    # one sent over and over, and the only one whose channel comes off a database row.
    for channel, address in ((Channel.NTFY, topic), (Channel.EMAIL, "a@example.org")):
        warning = alert_message(
            None,
            settings,
            SimpleNamespace(id=uuid.uuid4(), address=address, channel=channel),
            SimpleNamespace(timezone="Europe/Berlin"),
            {
                "predicted_start_at": "2026-09-23T14:30:00+00:00",
                "cycle_time": "2026-09-23T14:00:00+00:00",
                "lead_minutes": 30,
                "peak_mm_5min": 0.4,
            },
        )
        assert warning.channel == channel.value

    assert confirmation_message(settings, topic, "tok", channel="ntfy").channel == "ntfy"
    assert confirmation_message(settings, "a@example.org", "tok").channel == "email"
    assert settings_anchor_message(settings, topic, "tok", uuid.uuid4()).channel == "ntfy"
    assert (
        manage_link_message(settings, topic, "tok", uuid.uuid4(), channel="ntfy").channel == "ntfy"
    )
    assert manage_link_message(settings, "a@example.org", "tok", uuid.uuid4()).channel == "email"
    assert deletion_receipt(settings, topic, channel="ntfy").channel == "ntfy"
    assert deletion_receipt(settings, "a@example.org").channel == "email"


# --- a deployment with push working and no mail provider ------------------------------------


@pytest.fixture()
def push_only(db, settings):
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    only = settings.model_copy(update={"email_channel_enabled": False})
    return TestClient(create_app(only, session_factory=db, notifier=ConsoleNotifier()))


def test_the_email_channel_can_be_refused_before_anything_is_stored(push_only, db):
    """Accepting an address a deployment cannot write to is worse than refusing it: the page
    says to check a mailbox, and nothing ever arrives."""
    response = push_only.post(
        "/api/v1/subscriptions",
        json={"channel": "email", "email": "someone@example.org", "lat": 50.11, "lon": 8.68},
    )
    assert response.status_code == 422
    assert "Push" in response.json()["detail"]

    with db() as session:
        assert session.execute(select(Subscriber)).scalars().all() == []


def test_push_still_works_when_email_is_off(push_only):
    response = push_only.post(
        "/api/v1/subscriptions", json={"channel": "ntfy", "lat": 50.11, "lon": 8.68}
    )
    assert response.status_code == 202


def test_the_page_stops_offering_a_choice_it_would_reject(push_only, client):
    """The radio is not merely ignored - the question disappears, because its second answer is
    refused at the API."""
    offered = client.get("/").text
    assert 'value="email"' in offered

    push_only_body = push_only.get("/").text
    assert 'value="email"' not in push_only_body
    # The push radio stays checked and in the DOM: it is what the page's own script reads.
    assert 'value="ntfy" checked' in push_only_body


def test_an_existing_email_subscriber_can_still_reach_their_settings(push_only):
    """The flag gates signing up, not delivery. Someone subscribed before it was set still has
    a mailbox we send to, and locking them out of the settings page would be a worse bug than
    the one the flag fixes."""
    assert 'value="email"' in push_only.get("/manage").text
