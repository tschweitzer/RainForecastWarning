"""Development adapter: writes every message to a text file instead of sending it.

For seeing exactly what would be pushed - title, body, tap target - without a browser or a push
service in the loop. `rainalert outbox` prints the newest ones with their links.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from rainalert.notify.base import DeliveryResult, OutboundMessage


class FileNotifier:
    def __init__(self, outbox: str | Path) -> None:
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)

    def send(self, message: OutboundMessage) -> DeliveryResult:
        self.outbox.mkdir(parents=True, exist_ok=True)  # the directory may have gone since init
        name = f"{uuid.uuid4()}.txt"
        lines = [
            f"To: {message.to}",
            f"Title: {message.subject}",
            f"Link: {message.click_url or ''}",
            "",
            message.text,
            "",
        ]
        (self.outbox / name).write_text("\n".join(lines), encoding="utf-8")
        return DeliveryResult(ok=True, provider_message_id=name)
