"""SMTP adapter - the one that talks to every provider.

Brevo, Mailgun, SendGrid, Postmark, SES and an ordinary mailbox all accept SMTP, so choosing a
provider is a matter of host, port and credentials rather than a code change. That is the whole
reason this is the default production adapter (see rainalert/notify/__init__.py).
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from rainalert.notify.base import DeliveryResult, OutboundMessage


class SMTPNotifier:
    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        *,
        use_tls: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, message: OutboundMessage) -> DeliveryResult:
        msg = EmailMessage()
        msg["To"] = message.to
        msg["Subject"] = message.subject
        for key, value in message.headers.items():
            msg[key] = value
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")

        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            return DeliveryResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return DeliveryResult(ok=True)
