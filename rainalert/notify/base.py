"""The notifier protocol and the message it carries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class MessageAction:
    """A tappable button on a push notification, which POSTs to us and stays in the app.

    Only push has these; email ignores them. The point of the POST is that the reader never
    leaves the notification shade to reach us - the alternative, a link, means a browser, and
    a browser means the token lands in a URL bar and a history entry.
    """

    label: str
    url: str
    #: Already encoded for ``content_type`` - the renderer does not encode anything.
    body: str = ""
    content_type: str = "application/json"

    def __post_init__(self) -> None:
        # These become one header, in a format whose separators are the comma and the semicolon.
        # Quoting around them is possible but the values here are a fixed label, our own URL and
        # a signed token, none of which has any business containing either - so refuse rather
        # than quote, and keep the header shape beyond argument.
        for value in (self.label, self.url, self.body, self.content_type):
            if any(char in value for char in ",;\r\n"):
                raise ValueError("action fields must not contain a comma or semicolon")
            # A quote is only a delimiter where a value *begins* - ntfy's own documented example
            # passes `body={"action": "close"}` unquoted, so quotes inside a value are ordinary
            # characters. One at the front would be read as opening a quoted value instead.
            if value[:1] in ('"', "'"):
                raise ValueError("action fields must not begin with a quote")


@dataclass(frozen=True)
class OutboundMessage:
    to: str
    subject: str
    text: str
    #: Which kind of address ``to`` is, matching the subscriber's stored ``Channel``. Not a
    #: preference and not decoration: it is what lets one process hold both transports without
    #: an address ever reaching the wrong one. An ntfy topic is a path segment on a public
    #: server, so a mailbox delivered there would publish the address *as a topic name* and its
    #: confirmation link as that topic's contents.
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

    def __post_init__(self) -> None:
        # Header injection: a newline in a field that becomes a header lets an attacker append
        # headers of their own - Bcc, Reply-To, a second body. Addresses come from user input.
        for value in (self.to, self.subject, self.click_url or "", *self.headers.values()):
            if "\n" in value or "\r" in value:
                raise ValueError("header values must not contain line breaks")


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None


class Notifier(Protocol):
    def send(self, message: OutboundMessage) -> DeliveryResult: ...


class PushNotifier:
    """Placeholder for the future mobile app (D-15). Present so the interface is honest."""

    def send(self, message: OutboundMessage) -> DeliveryResult:
        raise NotImplementedError("push delivery arrives with the mobile app (M7)")
