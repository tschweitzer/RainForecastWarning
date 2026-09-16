"""Outbound notifications.

One protocol, several adapters, chosen by configuration. The provider decision is deliberately
*not* baked into the code: every transactional mail provider worth using (Brevo, Mailgun, SendGrid,
Postmark, SES, or a plain mailbox) speaks SMTP, so the SMTP adapter covers all of them and the
choice reduces to credentials in the environment. A provider's HTTP API can be added later behind
the same protocol if their delivery telemetry turns out to be worth the coupling.
"""

from typing import TYPE_CHECKING

from rainalert.notify.base import (
    DeliveryResult,
    Notifier,
    OutboundMessage,
    PushNotifier,
)
from rainalert.notify.console import ConsoleNotifier
from rainalert.notify.file import FileNotifier
from rainalert.notify.smtp import SMTPNotifier

if TYPE_CHECKING:
    from rainalert.config import Settings

__all__ = [
    "ConsoleNotifier",
    "DeliveryResult",
    "FileNotifier",
    "Notifier",
    "OutboundMessage",
    "PushNotifier",
    "SMTPNotifier",
    "build_notifier",
]


def build_notifier(kind: str, settings: "Settings") -> Notifier:
    """Pick an adapter by name. Unknown names fail loudly rather than silently not sending."""
    if kind == "console":
        return ConsoleNotifier()
    if kind == "file":
        return FileNotifier(settings.mail_outbox_dir or "./var/outbox")
    if kind == "smtp":
        return SMTPNotifier(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            timeout=settings.smtp_timeout_seconds,
        )
    if kind == "push":
        return PushNotifier()
    raise ValueError(f"unknown notifier {kind!r}")
