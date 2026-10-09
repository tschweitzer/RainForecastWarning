"""Outbound notifications.

One protocol, several adapters, chosen by configuration. The provider decision is deliberately
*not* baked into the code: every transactional mail provider worth using (Brevo, Mailgun, SendGrid,
Postmark, SES, or a plain mailbox) speaks SMTP, so the SMTP adapter covers all of them and the
choice reduces to credentials in the environment. A provider's HTTP API can be added later behind
the same protocol if their delivery telemetry turns out to be worth the coupling.
"""

import logging
from typing import TYPE_CHECKING

from rainalert.notify.base import (
    DeliveryResult,
    Notifier,
    OutboundMessage,
    PushNotifier,
)
from rainalert.notify.console import ConsoleNotifier
from rainalert.notify.file import FileNotifier
from rainalert.notify.routing import RoutingNotifier
from rainalert.notify.smtp import SMTPNotifier
from rainalert.notify.webpush import WebPushNotifier

if TYPE_CHECKING:
    from rainalert.config import Settings

__all__ = [
    "ConsoleNotifier",
    "DeliveryResult",
    "FileNotifier",
    "Notifier",
    "OutboundMessage",
    "PushNotifier",
    "RoutingNotifier",
    "SMTPNotifier",
    "WebPushNotifier",
    "build_notifier",
]


logger = logging.getLogger(__name__)


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
    if kind == "webpush":
        return WebPushNotifier(
            vapid_private_key=settings.vapid_private_key,
            vapid_subject=settings.vapid_subject,
            ttl_seconds=settings.webpush_ttl_seconds,
            timeout=settings.webpush_timeout_seconds,
        )
    if kind == "push":
        return PushNotifier()
    if kind == "auto":
        # What production wants: both channels live, each on its own transport. The dev kinds
        # above stay single adapters on purpose - `console` and `file` are sinks, and a local run
        # must not start posting to real push services because a test subscriber happened to pick
        # push.
        transports: dict[str, Notifier] = {"email": build_notifier("smtp", settings)}
        try:
            transports["webpush"] = build_notifier("webpush", settings)
        except ValueError:
            # Logged, not raised, and the reasoning is the same as the public-key derivation in
            # `api/app.py`: the map, the radar, the privacy page and the email channel all work
            # without push, and a service that refuses to start because one channel is misconfigured
            # takes the others down with it.
            #
            # That comment was already in `app.py` while this function made it false. `create_app`
            # builds the notifier before it reaches the guarded derivation, so on `NOTIFIER=auto`
            # with an empty or malformed VAPID key - a secret version that is blank, disabled, or
            # unreadable by the runtime service account, or a revision deployed before the secret
            # exists - the process raised at import. On Cloud Run that is a crash loop in which no
            # revision ever becomes ready and the whole site is down, push or not.
            #
            # Omitting the transport is the right degradation because `RoutingNotifier` already
            # refuses per *message* for a channel it does not have. A push subscriber's warning
            # fails and is recorded as failed; everyone else is unaffected.
            logger.exception(
                "web push transport unavailable - the push channel is disabled, "
                "email and the rest of the site continue"
            )
        return RoutingNotifier(**transports)
    raise ValueError(f"unknown notifier {kind!r}")
