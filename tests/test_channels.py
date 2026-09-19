"""Subscribing over a push channel (D-5 revised).

Double opt-in was never about email specifically: it is about proving the channel reaches the
person who asked. These tests are mostly about the ways that can go wrong differently for a push
topic than for a mailbox.
"""

import httpx
import pytest
from sqlalchemy import select

from rainalert import subscriptions as svc
from rainalert.config import Settings
from rainalert.db.models import Channel, Subscriber, SubscriptionStatus
from rainalert.notify.base import OutboundMessage
from rainalert.notify.ntfy import NtfyNotifier
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
    assert message.click_url == "https://rain.example/confirm?token=tok123"
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
    import re

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
    token = re.search(r"token=([^&]+)", link).group(1)
    assert client.get(f"/confirm?token={token}").status_code == 200  # the page, changes nothing
    assert client.post("/confirm", data={"token": token}).status_code == 200

    with db() as session:
        subscriber = session.execute(
            select(Subscriber).where(Subscriber.address_hash == hash_address("ntfy", topic))
        ).scalar_one()
        assert subscriber.confirmed_at is not None
        assert subscriber.subscriptions[0].status is SubscriptionStatus.ACTIVE


def test_the_qr_endpoint_refuses_to_encode_anything_else(client):
    """It takes text from a query string and hands it to a camera.

    Unrestricted, the site would vouch for any URL anyone put in front of it.
    """
    from rainalert.config import Settings

    server = Settings(database_url="postgresql+psycopg://x", _env_file=None).ntfy_server
    ok = client.get("/qr", params={"text": f"{server}/rainalert-abc"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/svg+xml")
    assert ok.headers["Cache-Control"] == "no-store"  # the topic is in the URL

    for hostile in ("https://evil.example/pay", "javascript:alert(1)", "x" * 600):
        assert client.get("/qr", params={"text": hostile}).status_code == 400


def test_the_phone_can_subscribe_without_scanning_its_own_screen(client):
    """Signing up on the phone you want warned is the normal case, and the QR is useless there.

    Copying the topic is the path that works on every platform regardless of what the app
    registered as a link handler, so it leads; the QR is folded away for the desktop case.
    """
    body = client.get("/").text
    assert "navigator.clipboard.writeText" in body
    assert "Kopieren" in body
    # the QR is behind a disclosure rather than in the way
    assert "createElement('details')" in body
    assert "Auf einem anderen Gerät abonnieren" in body


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
