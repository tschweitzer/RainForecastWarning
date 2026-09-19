"""Push delivery via ntfy (https://ntfy.sh).

Chosen for a reason that is about the product rather than convenience: a rain warning is only
useful before the rain. Email latency is unpredictable - usually seconds, sometimes minutes, and
greylisting can cost five - and a warning with a fifteen-minute lead time does not survive that.
A push reaches the phone in about a second and the delivery path is one HTTP POST we can see fail.

It also needs no domain, no provider contract and no credentials, which is why it can be tested
today while the mail questions are still open.
"""

from __future__ import annotations

import httpx

from rainalert.notify.base import DeliveryResult, OutboundMessage


class NtfyNotifier:
    def __init__(
        self,
        server: str = "https://ntfy.sh",
        *,
        token: str | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def send(self, message: OutboundMessage) -> DeliveryResult:
        """Publish to the subscriber's topic.

        ``message.to`` is the topic. It is put in the URL path rather than the ``Topic`` header
        because a header is a place a newline could smuggle something else; ``OutboundMessage``
        already refuses line breaks, and this keeps the second line of defence in the URL
        encoding rather than in the header parser.
        """
        headers = {"Title": message.subject}
        if message.click_url:
            headers["Click"] = message.click_url
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Unsubscribe is not a mail header here, but the link still belongs in the body so the
        # reader has it without going to the website.
        unsubscribe = message.headers.get("List-Unsubscribe", "").strip("<>")

        body = message.text
        if unsubscribe and unsubscribe not in body:
            body = f"{body}\n\nAbmelden: {unsubscribe}"

        try:
            response = self._client.post(
                f"{self.server}/{message.to}",
                content=body.encode("utf-8"),
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return DeliveryResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        if response.status_code >= 400:
            # The body carries ntfy's own explanation, which is the useful half of a 4xx.
            return DeliveryResult(
                ok=False, error=f"ntfy said {response.status_code}: {response.text[:200]}"
            )
        return DeliveryResult(ok=True, provider_message_id=response.headers.get("X-Message-Id"))
