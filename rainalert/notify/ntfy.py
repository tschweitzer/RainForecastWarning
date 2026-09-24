"""Push delivery via ntfy (https://ntfy.sh).

Chosen for a reason that is about the product rather than convenience: a rain warning is only
useful before the rain. Email latency is unpredictable - usually seconds, sometimes minutes, and
greylisting can cost five - and a warning with a fifteen-minute lead time does not survive that.
A push reaches the phone in about a second and the delivery path is one HTTP POST we can see fail.

It also needs no domain, no provider contract and no credentials, which is why it can be tested
today while the mail questions are still open.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode, urlparse

import httpx

from rainalert.notify.base import DeliveryResult, MessageAction, OutboundMessage

#: What the topic is called in the app's subscription list. Without it the entry is the raw
#: generated topic - `rainalert-94RPFjNgVX2YthqV6pqRw` - which is unguessable on purpose and
#: unreadable as a consequence.
DISPLAY_NAME = "Regenwarnung"

#: What ntfy renders. Documented in publish.md as "up to three user actions per notification".
MAX_ACTIONS = 3


def deep_link(server: str, topic: str, display: str | None = DISPLAY_NAME) -> str:
    """An `ntfy://` URL that opens the app on this topic and subscribes it.

    The form comes from ntfy's own documentation: `ntfy://<host>/<topic>` opens the app's detail
    view and "subscribes to the topic if not already subscribed", with `?secure=false` for a
    server reached over http and `?display=` for the name shown in the list.

    Deliberately **not** `https://<host>/<topic>`, which is what this used to offer: the same
    docs say "Android deep linking of http/https links is very brittle and limited", so that
    form generally lands on ntfy's web page rather than opening the app. It stays available as
    the fallback, because a custom scheme does nothing at all when the app is missing.
    """
    parsed = urlparse(server)
    host = parsed.netloc or parsed.path.strip("/")
    if not host:
        raise ValueError(f"cannot derive a host from {server!r}")

    query: dict[str, str] = {}
    # https is the default the app assumes, so the parameter is only needed to say otherwise -
    # which a self-hosted server on plain http does need.
    if parsed.scheme == "http":
        query["secure"] = "false"
    if display:
        query["display"] = display

    link = f"ntfy://{host}/{quote(topic, safe='')}"
    return f"{link}?{urlencode(query)}" if query else link


def _actions_header(actions: tuple[MessageAction, ...]) -> str:
    """ntfy's `Actions` header, short form.

    `http, <label>, <url>, method=POST, body=<body>` per ntfy's publish docs, actions separated
    by semicolons. `MessageAction` has already refused any value containing a separator, so no
    quoting is needed and none is emitted.

    ntfy renders at most three buttons; more are not an error, they are silently dropped, which
    would be a feature that looks present and is not. Nothing here sends more than one, so this
    only has to not lie about it.
    """
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"ntfy shows at most {MAX_ACTIONS} actions, got {len(actions)}")
    return "; ".join(
        f"http, {action.label}, {action.url}, method=POST, "
        f"headers.Content-Type={action.content_type}, body={action.body}"
        for action in actions
    )


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
        if message.actions:
            headers["Actions"] = _actions_header(message.actions)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Unsubscribe is not a mail header here, but the link still belongs in the body so the
        # reader has it without going to the website.
        #
        # Matched on the label rather than on the header's URL. The two agree today - D-33
        # dropped one-click, so `List-Unsubscribe` carries the same fragment link the body does -
        # but a body that already ends in "Abmelden:" is the thing not to duplicate, whatever
        # shape the header happens to be in.
        unsubscribe = message.headers.get("List-Unsubscribe", "").strip("<>")

        body = message.text
        if unsubscribe and "Abmelden:" not in body:
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
