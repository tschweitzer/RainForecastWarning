"""The notifier protocol and the message it carries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class MessageAction:
    """A tappable button on a push notification, which POSTs to us and stays in the app.

    Only push has these; email ignores them. The point of the POST is that the reader never
    leaves the notification shade to reach us - the alternative, a link, means a browser, and
    a browser means the token lands in a URL bar and a history entry. Under web push that
    guarantee is ours to keep rather than the transport's: the service worker runs `fetch()` in
    the background, so nothing opens and nothing is navigated.

    The comma, semicolon and leading-quote rules that used to be here went with ntfy. They existed
    because ntfy packed every action into one `Actions:` header whose separators those were. A web
    push payload is JSON, which needs no such care, and forbidding a comma meant no button could
    ever be labelled "Ja, abmelden".
    """

    label: str
    url: str
    #: Already encoded for ``content_type`` - the renderer does not encode anything.
    body: str = ""
    content_type: str = "application/json"

    def __post_init__(self) -> None:
        # Still refused: control characters. `url` reaches the service worker's fetch() and these
        # values are ours rather than a subscriber's, so anything here is a bug in our own message
        # building - which should fail where it is written, not where it is displayed.
        for value in (self.label, self.url, self.body, self.content_type):
            if any(char in value for char in "\r\n\x00"):
                raise ValueError("action fields must not contain control characters")


@dataclass(frozen=True)
class OutboundMessage:
    to: str
    subject: str
    text: str
    #: Which kind of address ``to`` is, matching the subscriber's stored ``Channel``. Not a
    #: preference and not decoration: it is what lets one process hold both transports without an
    #: address ever reaching the wrong one. The failure it was introduced to prevent was a mailbox
    #: delivered to ntfy, which published the address *as a public topic name*. Web push cannot
    #: repeat that exact mistake - an endpoint is issued by a browser, not chosen - but a mailbox
    #: handed to the web push transport is still a message nobody receives, silently.
    channel: str = "email"
    html: str | None = None
    #: Extra headers. One-click unsubscribe (RFC 8058) lives here.
    headers: dict[str, str] = field(default_factory=dict)
    #: Where tapping the message should take the reader. Email puts the link in the body and
    #: ignores this; push notifications have nowhere to put a link *except* here, so a
    #: confirmation that works in mail and not on a phone is exactly what this prevents.
    click_url: str | None = None
    #: Buttons rendered on a push notification. Email has nowhere to put these and drops them.
    actions: tuple[MessageAction, ...] = ()
    #: How long a push service should hold this message for a device that is offline (RFC 8030 §5.2),
    #: and how hard it should try to wake it (§5.3). `None` means "use the notifier's default", which
    #: is tuned for a rain warning.
    #:
    #: Per-message because one number cannot be right for every kind. The sender-wide 30 minutes is
    #: correct for a warning - `dispatcher.MAX_NOTIFICATION_AGE` refuses to send one older than that,
    #: because a late rain warning is worse than none - and wrong for everything else. A confirmation
    #: is valid for `confirm_token_ttl_hours` (24 h) and was being discarded by the push service after
    #: 30 minutes: a reader who signed up and then went through a long tunnel got nothing, was told by
    #: the page that their notifications must be misconfigured, and had a perfectly good 24-hour token
    #: nobody could deliver. Same for a settings link, and the liveness ping is the one message that
    #: genuinely does not deserve `Urgency: high`.
    ttl_seconds: int | None = None
    #: RFC 8030 §5.3: "high" wakes a dozing device, "normal" waits for it. `None` means the
    #: notifier's default.
    urgency: str | None = None
    #: Which notifications replace each other in the shade. `None` leaves it to the service worker's
    #: default.
    #:
    #: One tag for everything meant a settings link - or the worker's own acknowledgement of the
    #: button press that asked for it - evicted a *live rain warning*, with its map link, at the
    #: moment that warning was most wanted. Collapsing repeats of the same kind is the point; letting
    #: housekeeping displace the thing this service exists to deliver is not.
    push_tag: str | None = None

    def _check_push_fields(self) -> None:
        """`ttl_seconds` and `urgency` are written to RFC 8030 headers, so a nonsense value is a 400
        from the push service - i.e. a warning nobody receives, discovered in a log if at all.

        Only this codebase sets them today, which is the argument for checking them here rather than
        trusting each call site: the cost is one comparison and the failure mode is silent.
        """
        if self.ttl_seconds is not None and self.ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        if self.urgency is not None and self.urgency not in ("very-low", "low", "normal", "high"):
            raise ValueError(f"urgency must be an RFC 8030 value, got {self.urgency!r}")

    #: The browser's P-256 public key and the shared auth secret, base64url, straight from the
    #: subscriber row. Required for `channel="webpush"` and meaningless otherwise: RFC 8291
    #: encrypts to them, so a message without them cannot be built rather than being sent in
    #: clear. Channel-specific fields on a general message are not new here - `click_url` and
    #: `actions` are already push-only - and the alternative, packing three values into `to`,
    #: would put parsing between us and the endpoint we have to reach exactly.
    push_p256dh: str | None = None
    push_auth: str | None = None

    def __post_init__(self) -> None:
        # Header injection: a newline in a field that becomes a header lets an attacker append
        # headers of their own - Bcc, Reply-To, a second body. Addresses come from user input.
        for value in (self.to, self.subject, self.click_url or "", *self.headers.values()):
            if "\n" in value or "\r" in value:
                raise ValueError("header values must not contain line breaks")
        # A web push message without keys cannot be encrypted, so it cannot be sent. Refused here
        # rather than at the transport because *here* is where the mistake is made and where it is
        # cheap to see: every message builder runs in the test suite, so a builder that forgets the
        # keys fails immediately instead of shipping a channel that silently delivers nothing.
        #
        # This is not hypothetical. It is exactly what happened: `push_p256dh`/`push_auth` were
        # added to this dataclass and threaded through `subscriptions.subscribe` and the notifier,
        # and every one of the five builders in api/mail.py was left setting neither - so no push
        # subscriber could be confirmed, warned, sent a settings link or sent a receipt. The
        # transport reported it correctly and nothing was listening, because every test either
        # built its own message with keys or used a fake notifier that ignored them. 523 tests
        # passed over a channel that could not deliver one byte.
        if self.channel == "webpush" and not (self.push_p256dh and self.push_auth):
            raise ValueError(
                "a webpush message needs push_p256dh and push_auth - see Subscriber.push_p256dh"
            )
        self._check_push_fields()


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    #: The address is permanently dead and the row should be deleted, not retried. A push service
    #: answers 404 or 410 once a subscription has been revoked - which happens when the reader
    #: blocks notifications, clears site data, or uninstalls the browser. None of those reach us
    #: any other way, so this flag is the only signal that someone has unsubscribed by doing it
    #: in their browser instead of on our page, and it is what turns that into a deletion.
    gone: bool = False


class Notifier(Protocol):
    def send(self, message: OutboundMessage) -> DeliveryResult: ...


class PushNotifier:
    """Placeholder for a future *native* app (D-15), which web push does not replace.

    Kept distinct from `WebPushNotifier` on purpose. That one talks the W3C Push API to a browser;
    this one is the slot for FCM to an installed Android app, if that is ever built. Collapsing
    them would lose the distinction that decides which one a subscriber needs.
    """

    def send(self, message: OutboundMessage) -> DeliveryResult:
        raise NotImplementedError("native app delivery is not built (M7)")
