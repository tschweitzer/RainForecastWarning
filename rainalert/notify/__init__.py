"""Outbound notifications: web push, plus two sinks for development (D-76: push only).

One protocol, chosen by configuration: `webpush` in production, `console` or `file` locally.
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
    "UnavailableNotifier",
    "WebPushNotifier",
    "build_notifier",
]


logger = logging.getLogger(__name__)


class UnavailableNotifier:
    """Stands in for a web push transport that could not be built, and fails every send.

    The alternative is raising at start-up, and that was an outage once: `create_app` builds the
    notifier before anything else, so an empty or malformed VAPID key - a secret version that is
    blank, disabled, or unreadable by the runtime service account, or a revision deployed before the
    secret exists - crashed the process at import. On Cloud Run that is a crash loop in which no
    revision ever becomes ready and the map, the radar and the privacy page go down with push. This
    way the site keeps serving, and every message that cannot go out is recorded as failed, with the
    reason, where the dispatcher and the logs see it.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def send(self, message: OutboundMessage) -> DeliveryResult:
        return DeliveryResult(ok=False, error=f"web push transport unavailable: {self.reason}")


def build_notifier(kind: str, settings: "Settings") -> Notifier:
    """Pick an adapter by name. Unknown names fail loudly rather than silently not sending."""
    if kind == "console":
        return ConsoleNotifier()
    if kind == "file":
        return FileNotifier(settings.outbox_dir or "./var/outbox")
    if kind == "webpush":
        try:
            return WebPushNotifier(
                vapid_private_key=settings.vapid_private_key,
                vapid_subject=settings.vapid_subject,
                ttl_seconds=settings.webpush_ttl_seconds,
                timeout=settings.webpush_timeout_seconds,
            )
        except ValueError as exc:
            logger.exception("web push transport unavailable - nothing can be sent until it is")
            return UnavailableNotifier(str(exc))
    if kind == "push":
        return PushNotifier()
    raise ValueError(f"unknown notifier {kind!r}")
