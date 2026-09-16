"""Development adapter: writes .eml files you can open in a mail client.

More useful than the console one for checking how a message actually renders, including headers.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from pathlib import Path

from rainalert.notify.base import DeliveryResult, OutboundMessage


class FileNotifier:
    def __init__(self, outbox: str | Path) -> None:
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)

    def send(self, message: OutboundMessage) -> DeliveryResult:
        self.outbox.mkdir(parents=True, exist_ok=True)  # the directory may have gone since init
        msg = EmailMessage()
        msg["To"] = message.to
        msg["Subject"] = message.subject
        for key, value in message.headers.items():
            msg[key] = value
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")
        name = f"{uuid.uuid4()}.eml"
        (self.outbox / name).write_bytes(bytes(msg))
        return DeliveryResult(ok=True, provider_message_id=name)
