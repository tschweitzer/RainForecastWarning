"""Development adapter: prints instead of sending."""

from __future__ import annotations

from rainalert.notify.base import DeliveryResult, OutboundMessage


class ConsoleNotifier:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> DeliveryResult:
        self.sent.append(message)
        print(f"\n--- mail to {message.to} ---\n{message.subject}\n\n{message.text}\n---")
        return DeliveryResult(ok=True, provider_message_id=f"console-{len(self.sent)}")
