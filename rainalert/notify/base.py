"""The notifier protocol and the message it carries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class OutboundMessage:
    to: str
    subject: str
    text: str
    html: str | None = None
    #: Extra headers. One-click unsubscribe (RFC 8058) lives here.
    headers: dict[str, str] = field(default_factory=dict)
    #: Where tapping the message should take the reader. Email puts the link in the body and
    #: ignores this; push notifications have nowhere to put a link *except* here, so a
    #: confirmation that works in mail and not on a phone is exactly what this prevents.
    click_url: str | None = None

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
