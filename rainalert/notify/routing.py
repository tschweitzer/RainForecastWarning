"""One process, two transports, chosen by the message's own channel."""

from __future__ import annotations

from rainalert.notify.base import DeliveryResult, Notifier, OutboundMessage


class RoutingNotifier:
    """Send each message on the transport its channel names.

    Until this existed there was exactly one notifier per process, picked by ``NOTIFIER`` and
    applied to everything. That is fine while only one channel is live, and unsafe the moment
    both are. The failure that prompted it: with the old ``NOTIFIER=ntfy`` an email subscriber's
    confirmation was POSTed to ``<ntfy server>/<their address>``, publishing the address as a
    public topic name and the confirmation link as its contents. Refusing to send is the *good*
    outcome of a mismatch; delivering to the other transport is the bad one, and nothing was
    stopping it.

    The transports are ``email`` and ``webpush`` now, and the hazard is smaller but has not gone:
    a mailbox handed to the web push transport is refused by its endpoint check, but a message
    that reaches the wrong transport at all means somebody's warning was not delivered.

    A channel with no transport configured raises rather than falling back. A fallback here
    would be the same bug wearing a helpful face.
    """

    def __init__(self, **by_channel: Notifier) -> None:
        if not by_channel:
            raise ValueError("a routing notifier needs at least one transport")
        self._by_channel = by_channel

    def send(self, message: OutboundMessage) -> DeliveryResult:
        try:
            notifier = self._by_channel[message.channel]
        except KeyError:
            raise ValueError(
                f"no transport configured for channel {message.channel!r} "
                f"(have: {', '.join(sorted(self._by_channel))})"
            ) from None
        return notifier.send(message)
