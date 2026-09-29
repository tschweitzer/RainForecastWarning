"""The web push transport, the payload contract with the service worker, and the liveness job.

Three things here are worth more than the rest.

`test_the_browser_can_decrypt_what_we_send` is the only test that proves the crypto works. It plays
the browser's part - generates a P-256 keypair and an auth secret, hands us the public half, then
decrypts what comes back. Everything else about this transport could be right with the encryption
subtly wrong, and the symptom in production is not an error: the push service accepts the POST, the
browser fails to decrypt, and the reader is simply never warned.

`test_the_service_worker_reads_the_fields_the_payload_writes` is the seam between Python and
JavaScript. Nothing else checks it - the two files cannot import each other - so a renamed field
would be caught by nobody until a notification arrived blank.

`test_a_gone_subscription_is_deleted_rather_than_retried` covers the only way we ever learn that
somebody unsubscribed by blocking notifications or clearing their browser data.
"""

import base64
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from rainalert import subscriptions as svc
from rainalert.config import Settings
from rainalert.db.models import Channel, Notification, Subscriber
from rainalert.notify.base import DeliveryResult, MessageAction, OutboundMessage
from rainalert.notify.webpush import (
    ALLOWED_PUSH_HOSTS,
    MAX_ACTIONS,
    EndpointRefused,
    WebPushNotifier,
    b64url,
    check_endpoint,
    generate_vapid_keys,
    payload_for,
    unb64url,
)
from tests.helpers import page_source

SW = Path(__file__).resolve().parents[1] / "rainalert" / "api" / "static" / "sw.js"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
FRANKFURT = (50.1109, 8.6821)


@pytest.fixture(scope="module")
def vapid():
    private, public = generate_vapid_keys()
    return private, public


@pytest.fixture()
def browser():
    """The other end of RFC 8291: a P-256 keypair and a 16-byte auth secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {
        "key": key,
        "p256dh": b64url(public),
        "auth": b64url(b"0123456789abcdef"),
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


def notifier_for(vapid, handler, **kwargs):
    return WebPushNotifier(
        vapid_private_key=vapid[0],
        vapid_subject="mailto:ops@example.invalid",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def message_for(browser, **kwargs):
    defaults = {
        "to": ENDPOINT,
        "subject": "Regenwarnung",
        "text": "In etwa 25 Minuten faengt es an zu regnen.",
        "channel": "webpush",
        "click_url": "https://rain.example.invalid/map",
        "push_p256dh": browser["p256dh"],
        "push_auth": browser["auth"],
    }
    defaults.update(kwargs)
    return OutboundMessage(**defaults)


# --- the crypto ------------------------------------------------------------------------------


def test_the_browser_can_decrypt_what_we_send(vapid, browser):
    """The one test that proves this transport works at all.

    A wrong key derivation, a wrong content encoding or a reused ephemeral key all produce a POST
    the push service happily accepts and the browser silently cannot read - so asserting on our own
    output would prove nothing. This decrypts with the browser's private key instead.
    """
    captured = {}

    def handler(request):
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return httpx.Response(201, headers={"location": "https://fcm.googleapis.com/m/1"})

    result = notifier_for(vapid, handler).send(
        message_for(
            browser,
            actions=(MessageAction(label="Einstellungen", url="https://rain.example.invalid/x"),),
        )
    )
    assert result.ok

    # No `dh=`: aes128gcm carries the sender's public key in the payload's own header block, which
    # is also why no Crypto-Key header is sent.
    plaintext = http_ece.decrypt(
        captured["body"],
        private_key=browser["key"],
        auth_secret=unb64url(browser["auth"]),
        version="aes128gcm",
    )
    payload = json.loads(plaintext)
    assert payload["title"] == "Regenwarnung"
    assert payload["body"].startswith("In etwa 25 Minuten")
    assert payload["url"] == "https://rain.example.invalid/map"
    assert [a["title"] for a in payload["actions"]] == ["Einstellungen"]


def test_each_message_uses_a_fresh_ephemeral_key(vapid, browser):
    """RFC 8291 requires it. Reusing one would let the push service link two messages as coming
    from the same sender key, which is exactly the metadata the encryption is there to withhold."""
    bodies = []

    def handler(request):
        bodies.append(request.content)
        return httpx.Response(201)

    notifier = notifier_for(vapid, handler)
    notifier.send(message_for(browser))
    notifier.send(message_for(browser))
    # The ephemeral public key sits in the aes128gcm header: 16 bytes salt, 4 length, 1 idlen.
    assert bodies[0][21:86] != bodies[1][21:86]


def test_the_headers_are_rfc_8292_and_carry_no_crypto_key(vapid, browser):
    captured = {}

    def handler(request):
        captured.update(request.headers)
        return httpx.Response(201)

    notifier_for(vapid, handler).send(message_for(browser))
    assert captured["content-encoding"] == "aes128gcm"
    assert captured["authorization"].startswith("vapid t=")
    assert ",k=" in captured["authorization"]
    # Vapid01's "WebPush <jwt>" plus Crypto-Key is the older draft, and some services reject it
    # when sent alongside aes128gcm.
    assert "crypto-key" not in captured
    # Equal to dispatcher.MAX_NOTIFICATION_AGE. It was 3600, which contradicted it: the dispatcher
    # refuses to send a warning older than 30 minutes as stale, then asked the push service to hold
    # it for twice that - so a phone off-network for 50 minutes got a warning about rain that had
    # already passed, which is the case the shorter rule exists to prevent.
    from rainalert.alerting.dispatcher import MAX_NOTIFICATION_AGE

    assert captured["ttl"] == str(int(MAX_NOTIFICATION_AGE.total_seconds()))


def test_the_application_server_key_is_derived_from_the_private_one(vapid):
    """Configured separately they drift, and the failure is invisible: the browser subscribes fine
    with the wrong key and every later send is rejected as unauthorised."""
    notifier = WebPushNotifier(
        vapid_private_key=vapid[0], vapid_subject="mailto:ops@example.invalid"
    )
    assert notifier.application_server_key == vapid[1]
    # 65 raw bytes of uncompressed P-256 point, base64url without padding.
    assert len(base64.urlsafe_b64decode(vapid[1] + "==")) == 65


def test_a_subject_that_is_not_a_contact_is_refused(vapid):
    """RFC 8292's `sub` claim is a contact the push service uses to reach the operator. A malformed
    one is accepted by some services and rejected by others, which is the worst outcome: it works
    in testing and fails for a subset of subscribers."""
    for bad in ("ops@example.invalid", "", "tel:+491234"):
        with pytest.raises(ValueError, match="mailto:|VAPID"):
            WebPushNotifier(vapid_private_key=vapid[0], vapid_subject=bad)


# --- the endpoint allowlist ------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/x",
        "https://169.254.169.254/computeMetadata/v1/",
        "https://127.0.0.1:5432/",
        "https://fcm.googleapis.com.evil.test/x",
        "https://notfcm.googleapis.com/x",
        "https://evil.test/?u=https://fcm.googleapis.com/",
        "ftp://fcm.googleapis.com/x",
        "",
    ],
)
def test_check_endpoint_refuses_anything_that_is_not_a_push_service(endpoint):
    with pytest.raises(EndpointRefused):
        check_endpoint(endpoint)


@pytest.mark.parametrize("host", ALLOWED_PUSH_HOSTS)
def test_every_allowed_host_is_accepted_bare_and_as_a_subdomain(host):
    assert check_endpoint(f"https://{host}/x")
    assert check_endpoint(f"https://sub.{host}/x")


def test_the_notifier_checks_the_endpoint_even_if_the_row_got_in_another_way(vapid, browser):
    """Checked at subscribe time and again here. Twice on purpose: the first stops the row
    existing, the second means a row that arrived via a migration, a fixture or a hand-written
    INSERT still cannot turn into an outbound request to anywhere it likes."""
    called = []

    def handler(request):
        called.append(request.url)
        return httpx.Response(201)

    result = notifier_for(vapid, handler).send(
        message_for(browser, to="https://169.254.169.254/computeMetadata/v1/")
    )
    assert not result.ok
    assert not called, "the notifier made the request anyway"


# --- failure handling ------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "gone"), [(404, True), (410, True), (429, False), (500, False)])
def test_only_404_and_410_mean_the_subscription_is_gone(vapid, browser, code, gone):
    """429 and 500 are the push service having a bad day; deleting a subscriber over one would lose
    somebody who never unsubscribed."""
    notifier = notifier_for(vapid, lambda request: httpx.Response(code, text="x"))
    result = notifier.send(message_for(browser))
    assert not result.ok
    assert result.gone is gone


def test_a_webpush_message_without_keys_cannot_be_built(vapid):
    """Refused at construction, which is the only place it is cheap to notice.

    This is the defect that made the entire channel undeliverable: `push_p256dh`/`push_auth` were
    added to `OutboundMessage`, threaded through `subscriptions.subscribe` and the transport, and
    then every builder in api/mail.py was left setting neither. The transport reported it correctly
    and nobody was listening, because each test either built its own message with keys or used a
    fake notifier. 523 tests passed over a channel that could not deliver one byte.

    Now every builder runs through this guard in the suite, so forgetting is a failure rather than
    a silent outage.
    """
    with pytest.raises(ValueError, match="push_p256dh"):
        OutboundMessage(to=ENDPOINT, subject="s", text="t", channel="webpush")
    # Email is unaffected: it has no keys and needs none.
    OutboundMessage(to="a@b.example", subject="s", text="t")


def test_every_builder_that_can_target_push_carries_the_keys(settings):
    """The registry test. A builder added later shows up here as a KeyError rather than as a channel
    that quietly stops working - which is how the omission above survived."""
    from types import SimpleNamespace

    from rainalert.api.mail import (
        alert_message,
        confirmation_message,
        deletion_receipt,
        manage_link_message,
        push_keys,
    )
    from rainalert.jobs.liveness import liveness_message

    subscriber = SimpleNamespace(
        id=uuid.uuid4(),
        address=ENDPOINT,
        channel=Channel.WEBPUSH,
        push_p256dh="k" * 87,
        push_auth="a" * 22,
    )
    built = {
        "confirmation": confirmation_message(
            settings, ENDPOINT, "t", channel="webpush", subscriber=subscriber
        ),
        "manage link": manage_link_message(
            settings, ENDPOINT, "t", subscriber.id, channel="webpush", subscriber=subscriber
        ),
        "deletion receipt": deletion_receipt(
            settings, ENDPOINT, channel="webpush", push=push_keys(subscriber)
        ),
        "alert": alert_message(
            None,
            settings,
            subscriber,
            SimpleNamespace(timezone="Europe/Berlin"),
            {
                "predicted_start_at": "2026-09-27T14:25:00+00:00",
                "cycle_time": "2026-09-27T14:00:00+00:00",
                "lead_minutes": 25,
                "peak_mm_5min": 0.4,
                "timezone": "Europe/Berlin",
            },
        ),
        "liveness": liveness_message(settings, subscriber, "t"),
    }
    for name, message in built.items():
        assert message.channel == "webpush", name
        assert message.push_p256dh == "k" * 87, f"{name} lost the key"
        assert message.push_auth == "a" * 22, f"{name} lost the auth secret"


def test_unusable_keys_are_treated_as_gone(vapid, browser):
    """They come from a browser we cannot re-ask, so the row will fail forever. Reported as gone so
    the caller deletes it instead of retrying every five minutes."""
    notifier = notifier_for(vapid, lambda request: httpx.Response(201))
    result = notifier.send(message_for(browser, push_p256dh="not-a-key"))
    assert not result.ok
    assert result.gone


def test_a_network_failure_is_not_gone(vapid, browser):
    def handler(request):
        raise httpx.ConnectError("no route")

    result = notifier_for(vapid, handler).send(message_for(browser))
    assert not result.ok
    assert not result.gone


def test_an_error_body_is_truncated(vapid, browser):
    """A push service's error body is occasionally an HTML page, and the whole of it would land in
    `notifications.error` and in the logs."""
    notifier = notifier_for(vapid, lambda request: httpx.Response(400, text="x" * 5000))
    assert (
        len(
            notifier_for(vapid, lambda r: httpx.Response(400, text="x" * 5000))
            .send(message_for(browser))
            .error
        )
        < 400
    )
    del notifier


# --- the contract with sw.js ------------------------------------------------------------------


def test_more_actions_than_the_notification_api_renders_is_refused(browser):
    """`Notification.maxActions` is 2 and anything past that index is discarded silently at display
    time - a button that exists in the payload, is never drawn, and is a feature that looks present
    and is not."""
    action = MessageAction(label="x", url="https://rain.example.invalid/x")
    assert MAX_ACTIONS == 2
    with pytest.raises(ValueError, match="at most"):
        payload_for(message_for(browser, actions=(action,) * (MAX_ACTIONS + 1)))


def test_the_service_worker_reads_the_fields_the_payload_writes(browser):
    """The seam between Python and JavaScript, which nothing else checks.

    `payload_for` builds the JSON and `sw.js` consumes it, and the two files cannot import each
    other - so a renamed field is caught by no test and no type checker, and the symptom is a blank
    notification. Read out of the worker's source rather than restated here, so this fails when
    either side moves.
    """
    payload = json.loads(
        payload_for(
            message_for(
                browser,
                actions=(
                    MessageAction(
                        label="Einstellungen",
                        url="https://rain.example.invalid/x",
                        body='{"token":"t"}',
                    ),
                ),
            )
        )
    )
    source = SW.read_text(encoding="utf-8")
    for field in ("title", "body", "url", "actions"):
        assert field in payload, f"payload_for stopped writing {field}"
        assert f"data.{field}" in source, f"sw.js does not read data.{field}"
    for field in ("title", "url", "body", "contentType"):
        assert field in payload["actions"][0], f"payload_for stopped writing actions[].{field}"
        # Exactly `action.<field>`, with no alternatives. The first version of this allowed
        # `a.<field>` as well and exempted `title` outright - and `a.url`/`a.body` are substrings of
        # the `data.url`/`data.body` the push handler already contains, so renaming every action
        # field in sw.js left the test passing. Verified by mutation this time.
        assert f"action.{field}" in source, f"sw.js does not read action.{field}"
    # The action id is the array index, which is why payload_for must not reorder the array.
    assert "String(index)" in source


def test_the_service_worker_actually_passes_the_actions_to_the_notification(browser):
    """The buttons were computed and then not passed, so none was ever drawn.

    That single missing line removed the only route a push subscriber has from a notification into
    their settings - and `mail.py` had already dropped the unsubscribe URL from push bodies on the
    grounds that the Einstellungen button existed. It did not. Worse, the contract test next to this
    one passed throughout, because it greps for the field names and the unused mapping mentions all
    of them.

    This asserts the wiring rather than the vocabulary: `actions` must reach `showNotification`. The
    *behaviour* - which actions arrive, in what order, and capped to what the platform will draw -
    is asserted by running the worker in `tests/js/sw_test.mjs`, because the second half of this test
    used to be `assert "maxActions" in source` and that is worth spelling out as a lesson: it passed
    against `slice(0, 99)`, against slicing the wrong array, and against the real bug that shipped,
    which was `Notification.maxActions || 2` turning a platform reporting 0 into a request for 2.
    A grep for an identifier constrains nothing about what the code does with it.
    """
    source = SW.read_text(encoding="utf-8")
    options = source.split("showNotification(")[1].split("})")[0]
    assert "actions: actions" in options, "the actions never reach showNotification"


def test_the_tab_reuse_fix_has_both_of_its_halves(browser):
    """Two files have to agree for a tapped warning to land on the right place.

    `map.html` reads its `#l=` token on load and erases it, so a tab left from an earlier warning
    sits at plain `/map`; navigating it to `/map#l=<new token>` is a same-document navigation and no
    script re-runs. The fix is the `hashchange` listener on the page - and once that exists, the
    worker must NOT also route around the problem by opening a new window, which is what it used to
    do and which opened one more tab per warning.

    The worker's half is tested by running it (`tests/js/sw_test.mjs`, "a second warning does not
    open a second tab"). This asserts the page's half, which that harness cannot see, and it is a
    source check because the alternative is a full browser with a registered worker.
    """
    page = (
        Path(__file__).resolve().parents[1] / "rainalert" / "api" / "templates" / "map.html"
    ).read_text(encoding="utf-8")
    assert "hashchange" in page, "map.html must re-read the token when the fragment changes"
    # And the worker must not have grown the window-opening shortcut back.
    focus = SW.read_text(encoding="utf-8").split("function focusOrOpen(")[1]
    assert "break;" not in focus, (
        "focusOrOpen used to break out of the loop for a same-path target, which opened a new tab "
        "for every warning after the first - see tests/js/sw_test.mjs"
    )


def test_the_service_worker_is_served_from_the_root(browser):
    """A service worker's default scope is the directory it was served from, so /static/sw.js could
    register without complaint and then never receive a push for a notification shown on a real
    page."""
    from rainalert.api.app import create_app

    source = (Path(__file__).resolve().parents[1] / "rainalert" / "api" / "app.py").read_text()
    assert '@app.get("/sw.js"' in source
    del create_app


# --- pruning ---------------------------------------------------------------------------------


def test_a_gone_subscription_is_deleted_rather_than_retried(db, settings, vapid):
    """The only way we ever learn that somebody blocked notifications or cleared their site data.

    Without this the row stays, the endpoint is posted to every time it rains, and their
    coordinates are held indefinitely for a subscription that ended.
    """
    from rainalert.alerting.dispatcher import deliver_queued

    with db() as session:
        result = svc.subscribe(
            session,
            settings,
            channel=Channel.WEBPUSH,
            address=ENDPOINT,
            push_p256dh="k" * 87,
            push_auth="a" * 22,
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        subscriber = session.get(Subscriber, result.subscriber_id)
        subscriber.confirmed_at = datetime.now(UTC)
        subscription = subscriber.subscriptions[0]
        session.add(
            Notification(
                subscription_id=subscription.id,
                channel="webpush",
                status="queued",
                queued_at=datetime.now(UTC),
                payload={
                    "predicted_start_at": datetime.now(UTC).isoformat(),
                    "lead_minutes": 25,
                    "peak_mm_5min": 0.4,
                    "timezone": "Europe/Berlin",
                },
            )
        )
        session.commit()

        class Dead:
            def send(self, message):
                return DeliveryResult(ok=False, error="subscription gone (410)", gone=True)

        deliver_queued(session, settings, Dead())
        assert session.execute(select(Subscriber)).first() is None


def test_a_transient_failure_keeps_the_subscriber(db, settings):
    from rainalert.alerting.dispatcher import deliver_queued

    with db() as session:
        result = svc.subscribe(
            session,
            settings,
            channel=Channel.WEBPUSH,
            address=ENDPOINT,
            push_p256dh="k" * 87,
            push_auth="a" * 22,
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        subscriber = session.get(Subscriber, result.subscriber_id)
        subscriber.confirmed_at = datetime.now(UTC)
        session.add(
            Notification(
                subscription_id=subscriber.subscriptions[0].id,
                channel="webpush",
                status="queued",
                queued_at=datetime.now(UTC),
                payload={
                    "predicted_start_at": datetime.now(UTC).isoformat(),
                    "lead_minutes": 25,
                    "peak_mm_5min": 0.4,
                    "timezone": "Europe/Berlin",
                },
            )
        )
        session.commit()

        class Flaky:
            def send(self, message):
                return DeliveryResult(ok=False, error="push service said 503", gone=False)

        deliver_queued(session, settings, Flaky())
        assert session.execute(select(Subscriber)).first() is not None


# --- the liveness job ------------------------------------------------------------------------


def confirmed_push_subscriber(session, settings, *, confirmed_days_ago: int, endpoint=ENDPOINT):
    result = svc.subscribe(
        session,
        settings,
        channel=Channel.WEBPUSH,
        address=endpoint,
        push_p256dh="k" * 87,
        push_auth="a" * 22,
        lat=FRANKFURT[0],
        lon=FRANKFURT[1],
    )
    subscriber = session.get(Subscriber, result.subscriber_id)
    subscriber.confirmed_at = datetime.now(UTC) - timedelta(days=confirmed_days_ago)
    session.commit()
    return subscriber


def test_a_subscriber_who_has_never_been_warned_is_still_checked(db, settings):
    """The population this job exists for. Somebody whose threshold is never met gets no alerts, so
    nothing else would ever discover that their subscription died - and `NULL < cutoff` is NULL, so
    a naive query skips exactly them."""
    from rainalert.jobs.liveness import due_for_liveness

    with db() as session:
        confirmed_push_subscriber(session, settings, confirmed_days_ago=40)
        assert len(due_for_liveness(session, settings)) == 1


def test_a_recently_warned_subscriber_is_left_alone(db, settings):
    """ "If no alert was sent meanwhile" - a notification nobody needs is noise, and the alert
    already proved the subscription is alive."""
    from rainalert.jobs.liveness import due_for_liveness

    with db() as session:
        subscriber = confirmed_push_subscriber(session, settings, confirmed_days_ago=40)
        session.add(
            Notification(
                subscription_id=subscriber.subscriptions[0].id,
                channel="webpush",
                status="sent",
                queued_at=datetime.now(UTC) - timedelta(days=2),
                sent_at=datetime.now(UTC) - timedelta(days=2),
                payload={},
            )
        )
        session.commit()
        assert due_for_liveness(session, settings) == []


def test_a_fresh_subscriber_is_not_due(db, settings):
    from rainalert.jobs.liveness import due_for_liveness

    with db() as session:
        confirmed_push_subscriber(session, settings, confirmed_days_ago=3)
        assert due_for_liveness(session, settings) == []


def test_an_unconfirmed_subscriber_is_never_due(db, settings):
    """They delete themselves via unconfirmed_purge_hours, and nothing is ever sent to an address
    that has not confirmed."""
    from rainalert.jobs.liveness import due_for_liveness

    with db() as session:
        svc.subscribe(
            session,
            settings,
            channel=Channel.WEBPUSH,
            address=ENDPOINT,
            push_p256dh="k" * 87,
            push_auth="a" * 22,
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        assert due_for_liveness(session, settings) == []


def test_email_subscribers_are_never_due(db, settings):
    """A mailbox does not revoke itself, and an unsolicited monthly mail is closer to spam than to
    housekeeping."""
    from rainalert.jobs.liveness import due_for_liveness

    with db() as session:
        result = svc.subscribe(
            session,
            settings,
            channel=Channel.EMAIL,
            address="a@b.example",
            lat=FRANKFURT[0],
            lon=FRANKFURT[1],
        )
        session.get(Subscriber, result.subscriber_id).confirmed_at = datetime.now(UTC) - timedelta(
            days=90
        )
        session.commit()
        assert due_for_liveness(session, settings) == []


def test_the_liveness_run_sends_once_and_records_it(db, settings):
    """Recorded so that next month's query sees it - otherwise the same subscriber is notified
    every run, which is the opposite of monthly."""
    from rainalert.jobs.liveness import due_for_liveness, run_liveness

    sent_messages = []

    class Ok:
        def send(self, message):
            sent_messages.append(message)
            return DeliveryResult(ok=True, provider_message_id="m1")

    with db() as session:
        confirmed_push_subscriber(session, settings, confirmed_days_ago=40)
        sent, deleted = run_liveness(session, settings, Ok())
        assert (sent, deleted) == (1, 0)
        assert len(sent_messages) == 1
        assert sent_messages[0].channel == "webpush"
        assert [a.label for a in sent_messages[0].actions] == ["Einstellungen"]
        # And now nobody is due, because the send was recorded.
        assert due_for_liveness(session, settings) == []


def test_the_liveness_run_deletes_whoever_has_gone(db, settings):
    """The whole reason the job exists: scheduled deletion for somebody who cleared their browser
    data without telling us."""
    from rainalert.jobs.liveness import run_liveness

    class Dead:
        def send(self, message):
            return DeliveryResult(ok=False, error="subscription gone (410)", gone=True)

    with db() as session:
        confirmed_push_subscriber(session, settings, confirmed_days_ago=40)
        sent, deleted = run_liveness(session, settings, Dead())
        assert (sent, deleted) == (0, 1)
        assert session.execute(select(Subscriber)).first() is None


def test_a_liveness_send_that_fails_transiently_keeps_the_subscriber(db, settings):
    from rainalert.jobs.liveness import run_liveness

    class Flaky:
        def send(self, message):
            return DeliveryResult(ok=False, error="503", gone=False)

    with db() as session:
        confirmed_push_subscriber(session, settings, confirmed_days_ago=40)
        sent, deleted = run_liveness(session, settings, Flaky())
        assert (sent, deleted) == (0, 0)
        assert session.execute(select(Subscriber)).first() is not None


def test_the_liveness_notification_says_why_it_arrived(db, settings):
    """A notification nobody asked for has to explain itself in its first line, or it reads as the
    service malfunctioning."""
    from rainalert.jobs.liveness import liveness_message

    subscriber = type(
        "S", (), {"address": ENDPOINT, "push_p256dh": "k" * 87, "push_auth": "a" * 22}
    )()
    message = liveness_message(settings, subscriber, "tok")
    assert str(settings.webpush_liveness_days) in message.text
    # Says what the message is *for*, not what it is not - and does not claim there was no rain,
    # which the job cannot know: it measures the last message sent, not rainfall. Someone with a
    # high threshold through a wet month would have been told there was no rain to report.
    assert "prüft nur, ob wir dich noch erreichen" in message.text
    assert "keinen Regen" not in message.text
    assert "du musst nichts tun" in message.text
    assert "http" not in message.text


# --- endpoint rotation ----------------------------------------------------------------------


def test_nothing_moves_a_subscription_on_the_strength_of_an_endpoint():
    """There is no rotation endpoint, and there must not be one that works this way.

    `POST /api/v1/push/resubscribe` existed and was removed. It moved a confirmed subscriber to a
    new endpoint, authorised by possession of the *old* endpoint string and nothing else, on the
    premise that an endpoint is already a capability. The premise is false: a push service rejects a
    send whose VAPID signature does not match the key the subscription was created with, so knowing
    an endpoint lets a third party do nothing at all. The endpoint turned that string into the power
    to redirect somebody's warnings - and a warning carries a locate reference that resolves to
    exact coordinates, plus a token that opens their settings.

    Asserted as an absence because that is what it is. If a rotation route comes back it needs the
    *new* subscription to prove it is the same browser, which no bearer string can do.
    """
    from rainalert.api.app import create_app

    source = (Path(__file__).resolve().parents[1] / "rainalert" / "api" / "app.py").read_text()
    assert "resubscribe" not in source
    assert "ResubscribeRequest" not in source
    del create_app


def test_a_rotated_endpoint_is_pruned_rather_than_followed(db, settings):
    """What replaces it. The old endpoint answers 410 on the next send, the row is deleted, and the
    reader subscribes again - losing their settings, which is the cost D-47 already accepts."""
    from rainalert.alerting.dispatcher import deliver_queued

    with db() as session:
        subscriber = confirmed_push_subscriber(session, settings, confirmed_days_ago=1)
        session.add(
            Notification(
                subscription_id=subscriber.subscriptions[0].id,
                channel="webpush",
                status="queued",
                queued_at=datetime.now(UTC),
                payload={
                    "predicted_start_at": datetime.now(UTC).isoformat(),
                    "lead_minutes": 25,
                    "peak_mm_5min": 0.4,
                    "timezone": "Europe/Berlin",
                },
            )
        )
        session.commit()

        class Rotated:
            def send(self, message):
                return DeliveryResult(ok=False, error="subscription gone (410)", gone=True)

        deliver_queued(session, settings, Rotated())
        assert session.execute(select(Subscriber)).first() is None


# --- who may change a confirmed subscription -----------------------------------------------------


def test_knowing_the_endpoint_does_not_let_anyone_change_a_subscription(db, settings):
    """The hole that `POST /api/v1/push/resubscribe` was deleted for, reopened in `subscribe()`.

    A push endpoint is not a secret that proves anything: a push service will not deliver to it for
    anyone who lacks our VAPID key, so it is a username. `subscribe()` nevertheless treated a re-POST
    of a confirmed endpoint as the owning browser and overwrote the stored keys and location on the
    strength of it. One unauthenticated request could then:

    * replace the keys, so every later warning is encrypted to keys the reader's browser cannot
      decrypt while the push service still answers 201 - silence neither side can see, and which the
      liveness job cannot catch because it measures *successful* sends;
    * store an off-curve key, so the next send reports `gone` and deletes the subscriber;
    * move the stored home coordinates.

    What authenticates the owning browser is the pair it already holds - `auth` is a 16-byte secret,
    `p256dh` its public key, and neither is published or echoed back.
    """
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    client = TestClient(
        create_app(settings, session_factory=db, notifier=ConsoleNotifier()),
        base_url=settings.public_base_url,
    )
    with db() as session:
        subscriber = confirmed_push_subscriber(session, settings, confirmed_days_ago=1)
        original_keys = (subscriber.push_p256dh, subscriber.push_auth)
        original_place = (subscriber.subscriptions[0].lat, subscriber.subscriptions[0].lon)

    attacker = client.post(
        "/api/v1/subscriptions",
        json={
            "channel": "webpush",
            "lat": 52.52,
            "lon": 13.405,
            "endpoint": ENDPOINT,  # the only thing the attacker holds
            "p256dh": "A" * 87,
            "auth": "B" * 22,
        },
    )
    assert attacker.status_code == 202
    # Identical to a brand-new endpoint's answer, so this is not an oracle either.
    assert "already_active" not in attacker.json()

    with db() as session:
        subscriber = session.execute(select(Subscriber)).scalar_one()
        assert (subscriber.push_p256dh, subscriber.push_auth) == original_keys, "keys overwritten"
        assert (
            subscriber.subscriptions[0].lat,
            subscriber.subscriptions[0].lon,
        ) == original_place, "location moved"


def test_the_owning_browser_can_still_move_its_location(db, settings):
    """The other half: presenting the pair only that browser holds is how "I moved" works, and it is
    the only route a push subscriber has without a notification in hand."""
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    client = TestClient(
        create_app(settings, session_factory=db, notifier=ConsoleNotifier()),
        base_url=settings.public_base_url,
    )
    with db() as session:
        subscriber = confirmed_push_subscriber(session, settings, confirmed_days_ago=1)
        keys = (subscriber.push_p256dh, subscriber.push_auth)

    response = client.post(
        "/api/v1/subscriptions",
        json={
            "channel": "webpush",
            "lat": 48.1372,
            "lon": 11.5756,
            "endpoint": ENDPOINT,
            "p256dh": keys[0],
            "auth": keys[1],
        },
    )
    assert response.json()["already_active"] is True
    with db() as session:
        subscription = session.execute(select(Subscriber)).scalar_one().subscriptions[0]
        assert (round(subscription.lat, 4), round(subscription.lon, 4)) == (48.1372, 11.5756)


def test_half_the_pair_is_not_enough(db, settings):
    """`auth` is the secret half; a leaked `p256dh` alone must not be a credential."""
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    client = TestClient(
        create_app(settings, session_factory=db, notifier=ConsoleNotifier()),
        base_url=settings.public_base_url,
    )
    with db() as session:
        subscriber = confirmed_push_subscriber(session, settings, confirmed_days_ago=1)
        real_p256dh = subscriber.push_p256dh
        place = (subscriber.subscriptions[0].lat, subscriber.subscriptions[0].lon)

    client.post(
        "/api/v1/subscriptions",
        json={
            "channel": "webpush",
            "lat": 51.0,
            "lon": 7.0,
            "endpoint": ENDPOINT,
            "p256dh": real_p256dh,
            "auth": "WRONGwrongWRONGwrong12",
        },
    )
    with db() as session:
        subscription = session.execute(select(Subscriber)).scalar_one().subscriptions[0]
        assert (subscription.lat, subscription.lon) == place


# --- the page's own side ----------------------------------------------------------------------


def test_the_page_does_not_offer_push_without_a_vapid_key(db, settings):
    """A button that cannot work is worse than an absent one. With no key configured the page says
    so instead.

    Two assertions, because the key travels in two hops now. It used to be interpolated straight
    into the inline script as `var VAPID_KEY = "..."`, so one grep covered it; the script is a
    static file since the extraction, and the key reaches it through a `data-` attribute on
    `<body>`. Asserting only the empty attribute would pass on a page whose script had stopped
    reading that attribute, and asserting only the wiring would pass on a page that rendered a key
    it does not have. Neither half is worth anything alone.
    """
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    client = TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))

    html = client.get("/").text
    assert 'data-vapid-key=""' in html, "expected an empty key with none configured"

    script = client.get("/static/signup.js").text
    # Anchored at the end, because a plain substring check passes on `d.vapidKeyX` - which is a
    # different attribute (`data-vapid-key-x`), i.e. exactly the rename this is meant to catch.
    assert re.search(r"vapidKey: d\.vapidKey\b", script), (
        "the config has to come off the body's dataset"
    )
    assert re.search(r"var VAPID_KEY = CONFIG\.vapidKey\b", script), (
        "and the script has to read that config"
    )

    # And the message it would show must not tell the reader to pick an option that is absent: on a
    # push-only deployment there is no email radio to choose, which is the intended first shape.
    assert "nicht verfügbar" in script
    assert "EMAIL_AVAILABLE" in script, "the fallback suggestion has to be conditional"


def test_the_page_warns_that_clearing_browser_data_ends_the_subscription(db, settings):
    """D-47. The subscription and the settings session live in the same site-data bucket, and Chrome
    clears them together - there is no recovery path, so this is said before signing up rather than
    discovered afterwards."""
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    client = TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))
    # Whitespace-collapsed: the sentence wraps in the template, so a literal match would be
    # asserting on Jinja's indentation rather than on what the reader sees.
    body = " ".join(page_source(client).split())
    assert "Websitedaten löschst" in body
    assert "musst du dich neu anmelden" in body
    assert "Standort" in body and "löschen wir" in body
    # Not "dabei", which promises deletion at the moment the reader clears their data. We do not
    # find out until a send fails, and if nothing is sent the liveness ping is up to
    # `webpush_liveness_days` away with a weekly check behind it - which privacy.html documents as
    # "es kann also einige Tage länger dauern". Two pages must not disagree about a deletion
    # promise, and this is the page that was overclaiming.
    assert "Standort wird dabei" not in body


def test_the_confirmation_page_speaks_to_the_channel_that_confirmed(db, settings):
    """It branched on `channel == "ntfy"` for a while after the rename, so every push subscriber was
    told "Du bekommst jetzt eine Mail" on the first page they ever see - and the else branch also
    withheld the line explaining the Einstellungen button, which since D-45 is their only durable
    route back into settings. Both wrong, on the page that has to make a first impression.

    Driven through the real flow rather than by rendering the template: the channel value reaching
    it comes from `subscriber.channel.value`, and that is the half that changed.
    """
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    # ConsoleNotifier keeps every message it was given in `.sent`, which is what this needs.
    notifier = ConsoleNotifier()
    client = TestClient(
        create_app(settings, session_factory=db, notifier=notifier),
        base_url=settings.public_base_url,
    )
    client.post(
        "/api/v1/subscriptions",
        json={
            "channel": "webpush",
            "lat": FRANKFURT[0],
            "lon": FRANKFURT[1],
            "endpoint": ENDPOINT,
            "p256dh": "k" * 87,
            "auth": "a" * 22,
        },
    )
    token = notifier.sent[-1].click_url.split("/confirm#a=")[1]
    body = client.post("/confirm", data={"token": token}).text

    assert "eine Mail" not in body, "a push subscriber was promised email"
    assert "Benachrichtigung, wenn Regen aufzieht" in body
    # The line that tells them how to get back in, which the else branch used to suppress.
    assert "Knopf \u201aEinstellungen\u2018" in body or "Einstellungen" in body


def test_the_settings_page_offers_a_way_out(db, settings):
    """Since D-45 the only one on push: a notification body is plain text nothing linkifies, so the
    "Abmelden:" URL email still carries cannot be tapped there."""
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    client = TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))
    body = page_source(client, "/manage")
    assert 'id="delete"' in body
    assert 'id="delete-yes"' in body
    # Two steps: a single mis-tap next to "Sitzung beenden" must not delete an account.
    assert 'id="delete-confirm" hidden' in body
    assert "unsubscribe()" in body, "the browser's own subscription must be released too"


# --- malformed endpoints must refuse, never raise something else -----------------------------

#: Each of these makes a stdlib URL accessor raise a bare `ValueError`. They are here as a group
#: because the bug they cover recurred: the first round guarded `.port`, having found `:0x1bb`, and
#: left `urlparse` and `.hostname` raising. A parametrised list is the form that does not rot -
#: adding a case costs one line, and the guard is around the parse as a whole rather than around
#: each accessor, so a case nobody thought of is covered too.
ENDPOINTS_THAT_BREAK_THE_PARSER = [
    ("port is not a number", "https://fcm.googleapis.com:0x1bb/x"),
    ("port is out of range", "https://fcm.googleapis.com:99999999/x"),
    # Brackets mean an IP literal, so `.hostname` tries to parse a domain as an address.
    ("bracketed non-ip host", "https://[fcm.googleapis.com]/x"),
    # U+2100 NFKC-normalises to "a/c", so the netloc contains a delimiter after normalisation and
    # `urlparse` itself refuses it.
    ("nfkc expands to a delimiter", "https://℀.fcm.googleapis.com/x"),
    ("nfkc fullwidth number sign", "https://＃.fcm.googleapis.com/x"),
]


@pytest.mark.parametrize(
    ("label", "endpoint"),
    ENDPOINTS_THAT_BREAK_THE_PARSER,
    ids=[label for label, _ in ENDPOINTS_THAT_BREAK_THE_PARSER],
)
def test_an_endpoint_that_breaks_the_url_parser_is_refused_not_raised(label, endpoint):
    """`check_endpoint` promises to return or raise `EndpointRefused`, and that promise is what the
    callers are written against: `subscriptions.subscribe` catches `EndpointRefused`, the route
    catches `ValidationError`, and anything else escapes both as an unauthenticated 500.

    `EndpointRefused` subclasses `ValueError`, so this asserts the subclass specifically - matching
    on `ValueError` would pass against the bug.
    """
    from rainalert.notify.webpush import EndpointRefused, check_endpoint

    with pytest.raises(EndpointRefused):
        check_endpoint(endpoint)


def test_a_real_endpoint_still_passes_after_the_parse_guard():
    """The counterpart: wrapping the parse must not have swallowed the working case."""
    from rainalert.notify.webpush import check_endpoint

    endpoint = "https://fcm.googleapis.com/fcm/send/abc123"
    assert check_endpoint(endpoint) == endpoint


# --- comparing a secret must not be crashable by the secret ---------------------------------


def test_same_secret_matches_and_rejects():
    from rainalert.tokens import same_secret

    assert same_secret("abc", "abc")
    assert not same_secret("abc", "abd")
    assert not same_secret("abc", "")


@pytest.mark.parametrize("offered", ["ä" * 10, "日本語", "äbc", "a\u0000b"])
def test_same_secret_does_not_raise_on_non_ascii(offered):
    """`hmac.compare_digest` raises TypeError on a non-ASCII `str` - it will not guess an encoding
    at the cost of the constant-time guarantee. Every secret compared in this codebase arrives from
    a request, so that TypeError was an unauthenticated 500 on several routes at once. Encoding to
    bytes first means a non-ASCII offering simply fails to match."""
    from rainalert.tokens import same_secret

    assert not same_secret(offered, "abc")
    assert not same_secret("abc", offered)


def test_a_non_ascii_token_signature_is_rejected_not_a_crash():
    """The same bug on the token verifiers, which predate this change and are reachable from any
    unsubscribe link or settings token."""
    import uuid

    from rainalert.tokens import (
        manage_request_token,
        unsubscribe_token,
        verify_manage_request_token,
        verify_unsubscribe_token,
    )

    subscriber_id = uuid.uuid4()

    good = unsubscribe_token(subscriber_id, "secret")
    assert verify_unsubscribe_token(good, "secret") == subscriber_id
    assert verify_unsubscribe_token(f"{good.partition('.')[0]}.{'ä' * 20}", "secret") is None

    manage = manage_request_token(subscriber_id, "secret", 30)
    assert verify_manage_request_token(manage, "secret") is not None
    assert verify_manage_request_token(f"{manage.rsplit('.', 1)[0]}.{'ü' * 20}", "secret") is None


def test_a_failed_liveness_row_never_stays_queued(db, settings):
    """The invariant the code comment insists on, which nothing asserted.

    `queued` means "deliver_queued should send this", and that function only knows how to render rain
    warnings. A liveness row left queued was a `KeyError` that aborted the whole delivery run and
    rolled back the `sent` status of every warning already delivered in it: every five minutes, no
    warnings at all, and duplicate sends of the ones that had worked.
    """
    from rainalert.db.models import Notification
    from rainalert.jobs.liveness import run_liveness

    class Flaky:
        def send(self, message):
            return DeliveryResult(ok=False, error="503", gone=False)

    with db() as session:
        confirmed_push_subscriber(session, settings, confirmed_days_ago=40)
        run_liveness(session, settings, Flaky())
        rows = session.execute(select(Notification)).scalars().all()
        assert rows, "the run must leave a record of the subscriber it touched"
        for row in rows:
            assert row.status != "queued", "a liveness row must never be left for deliver_queued"


def test_deliver_queued_survives_a_row_it_cannot_render(db, settings):
    """The other half, from the dispatcher's side: a queued row that is not a rain warning must not
    take the run down, and must not sit there forever either.

    Both halves matter and only one of them was true. The filter stopped the crash; it *skipped* the
    row, so it stayed `queued` in the table and in the `notifications_pending` index, and every run
    for the rest of time walked past it again.
    """
    from rainalert.alerting.dispatcher import deliver_queued
    from rainalert.db.models import Notification, Subscription

    class Counting:
        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)
            return DeliveryResult(ok=True, provider_message_id="x")

    with db() as session:
        subscriber = confirmed_push_subscriber(session, settings, confirmed_days_ago=1)
        subscription = session.execute(select(Subscription)).scalars().one()
        session.add(
            Notification(
                subscription_id=subscription.id,
                event_id=None,
                channel="webpush",
                status="queued",
                queued_at=datetime.now(UTC),
                payload={"kind": "liveness", "days": 30},
            )
        )
        session.commit()
        notifier = Counting()
        # No exception, and nothing sent for a row it cannot render.
        deliver_queued(session, settings, notifier)
        assert notifier.sent == []
        row = session.execute(select(Notification)).scalars().one()
        assert row.status == "expired", "an unrenderable row must be expired, not skipped forever"
        assert row.error
        del subscriber


@pytest.mark.parametrize("hostile", ["\ud800", "abc\ud800", "\udfff\ud800"])
def test_a_lone_surrogate_cannot_crash_a_comparison_or_a_hash(hostile):
    """`str.encode("utf-8")` raises `UnicodeEncodeError` on an unpaired UTF-16 surrogate.

    The first version of the non-ASCII fix used a plain encode and its comment claimed the crash was
    fixed "for good"; it had swapped a wide `TypeError` for a narrow `UnicodeEncodeError`. Nothing
    reaches it through HTTP today - pydantic-core's JSON parser answers 422, Starlette decodes forms
    with `errors="replace"`, headers are latin-1 - but that is three unrelated layers holding a
    property these functions should hold themselves, and a different JSON parser would expose it.
    """
    from rainalert.tokens import hash_address, hash_token, same_secret

    assert same_secret(hostile, "abc") is False
    assert same_secret("abc", hostile) is False
    assert len(hash_token(hostile)) == 32
    assert len(hash_address("email", hostile)) == 32
    # Still a hash, not a constant: two different impossible strings must not collide.
    assert hash_token(hostile) != hash_token(hostile + "x")


@pytest.mark.parametrize(
    "endpoint",
    [
        # The label `169.254.169.254\` ends with `.fcm.googleapis.com`, so the suffix match accepted
        # it. Not an SSRF - the name does not resolve - but not a hostname either.
        "https://169.254.169.254\\.fcm.googleapis.com/x",
        "https://evil.test\\.fcm.googleapis.com/x",
        "https://a_b.fcm.googleapis.com/x",
        "https://fcm.googleapis.com./x",
    ],
)
def test_a_host_that_is_not_a_hostname_is_refused(endpoint):
    """The allowlist names six services; anything it accepts should at least be a syntactically
    possible host, so that the next reader of `host.endswith(...)` does not have to reason about why
    a backslash in a domain name happens to be harmless."""
    from rainalert.notify.webpush import EndpointRefused, check_endpoint

    with pytest.raises(EndpointRefused):
        check_endpoint(endpoint)


def test_the_worker_and_the_builders_agree_on_the_settings_tag():
    """`sw.js` tags the acknowledgement it shows itself, and `mail.py` tags the settings link that
    arrives a moment later. They have to be the same string or the link stops replacing the
    acknowledgement and the reader is left holding both - which is the bug the shared tag was
    introduced to fix, reappearing because two files spell a constant separately.

    They are separate constants rather than one because nothing crosses from Python into a service
    worker at build time; this test is the seam.
    """
    from rainalert.api.mail import ALERT_TAG, MANAGE_TAG

    source = SW.read_text(encoding="utf-8")
    assert f"var MANAGE_TAG = '{MANAGE_TAG}';" in source
    # And a warning must not share it, which is the whole point of having two.
    assert ALERT_TAG != MANAGE_TAG


def test_a_rain_warning_and_a_settings_link_do_not_evict_each_other(settings):
    """A settings link replacing a live warning takes the map link away at the moment it is wanted.

    Asserted on the two builders plus the payload the worker actually reads, so a tag that is set on
    the message and dropped on the way out still fails.
    """
    from types import SimpleNamespace

    from rainalert.api.mail import ALERT_TAG, MANAGE_TAG, alert_message, manage_link_message
    from tests.test_channels import ALERT_PAYLOAD, push_subscriber

    who = push_subscriber()
    warning = alert_message(
        None, settings, who, SimpleNamespace(timezone="Europe/Berlin"), ALERT_PAYLOAD
    )
    link = manage_link_message(
        settings, who.address, "tok", who.id, channel="webpush", subscriber=who
    )

    assert warning.push_tag == ALERT_TAG
    assert link.push_tag == MANAGE_TAG
    assert warning.push_tag != link.push_tag
    assert json.loads(payload_for(warning))["tag"] == ALERT_TAG
    assert json.loads(payload_for(link))["tag"] == MANAGE_TAG


def test_a_long_location_header_does_not_roll_back_a_liveness_run(db, settings):
    """`run_liveness` commits once at the end, so a `DataError` on that commit discards everything
    the run did - including the deletions it made for subscriptions the push service reported gone.

    The dispatcher truncates this value and this job did not, which is the kind of divergence that
    only shows up when a push service changes its header format.
    """
    from rainalert.db.models import Notification
    from rainalert.jobs.liveness import run_liveness

    class Chatty:
        def send(self, message):
            # Far longer than the String(256) column.
            return DeliveryResult(ok=True, provider_message_id="https://fcm.example/" + "x" * 400)

    with db() as session:
        confirmed_push_subscriber(session, settings, confirmed_days_ago=40)
        sent, deleted = run_liveness(session, settings, Chatty())
        assert (sent, deleted) == (1, 0)
        row = session.execute(select(Notification)).scalars().one()
        # The run survived, the row was recorded, and the value was cut to fit.
        assert row.status == "sent"
        assert row.sent_at is not None
        assert len(row.provider_message_id) == 256


def test_the_silent_subscriber_count_finds_someone_who_never_acts(db, settings):
    """The one signal for a push that is accepted and never displayed.

    A payload encrypted to the wrong keys still gets a 201, so `sent` is recorded, the reader sees
    nothing, and nothing else in this service can tell. What makes it countable is that every push
    carries an Einstellungen button, so a subscriber with successful sends and no MANAGE token ever
    issued has received messages and acted on none of them.
    """
    from datetime import UTC, datetime

    from rainalert.db.models import AuthToken, Notification, Subscription, TokenPurpose
    from rainalert.jobs.liveness import count_silent_subscribers
    from rainalert.tokens import hash_token

    with db() as session:
        subscriber = confirmed_push_subscriber(session, settings, confirmed_days_ago=40)
        subscription = session.execute(select(Subscription)).scalars().one()
        # Nothing sent yet: not silent, just new.
        assert count_silent_subscribers(session) == 0

        session.add(
            Notification(
                subscription_id=subscription.id,
                event_id=None,
                channel="webpush",
                status="sent",
                queued_at=datetime.now(UTC),
                sent_at=datetime.now(UTC),
                payload={"kind": "liveness"},
            )
        )
        session.commit()
        assert count_silent_subscribers(session) == 1, "sent to, never acted on"

        # They press Einstellungen once: no longer silent.
        session.add(
            AuthToken(
                subscriber_id=subscriber.id,
                purpose=TokenPurpose.MANAGE,
                token_hash=hash_token("t"),
                expires_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
        assert count_silent_subscribers(session) == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        # The host a real Chrome install produced, which this service refused. The shard number
        # varies per subscription, so an exact-host list can never cover the family.
        "https://jmt17.google.com/gcm/send/APA91bHun4MxP5egoKMwt2KZ",
        "https://jmt42.google.com/gcm/send/x",
        "https://jmt1.google.com/gcm/send/x",
        "https://gcm-http.googleapis.com/gcm/send/x",
    ],
)
def test_googles_sharded_push_hosts_are_accepted(endpoint):
    """Chrome hands out `jmt<n>.google.com`, and the allowlist did not have it.

    Every Chrome subscriber on such a shard could grant permission, watch their browser create a
    subscription, see the site listed under their notification settings - and be rejected here, with
    the page telling them to check input that was already correct. The list had been written from
    what the documentation says Chrome uses rather than from what Chrome emits.
    """
    from rainalert.notify.webpush import check_endpoint

    assert check_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://jmt17.google.com.evil.test/x",  # the shard name as a prefix of another domain
        "https://notjmt17.google.com/x",  # a longer label ending in the shard name
        "https://jmt.google.com/x",  # no digits: not a shard
        "https://jmtabc.google.com/x",  # letters where the digits go
        "https://sub.jmt17.google.com/x",  # a subdomain of a shard is not a shard
        "https://evil.google.com/x",  # google.com as a whole is NOT allowlisted
    ],
)
def test_the_shard_pattern_does_not_open_google_generally(endpoint):
    """The fix admits a family, not a domain. `fullmatch` on both ends is what keeps
    `jmt17.google.com.evil.test` out, and google.com at large is still refused."""
    from rainalert.notify.webpush import EndpointRefused, check_endpoint

    with pytest.raises(EndpointRefused):
        check_endpoint(endpoint)
