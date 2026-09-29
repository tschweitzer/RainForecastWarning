"""Web push delivery: RFC 8030 for the protocol, RFC 8291 for the payload, RFC 8292 for VAPID.

Replaced the ntfy transport on 2026-09-27 (D-45). The reason was not privacy but comprehensibility:
ntfy made the first thing a subscriber had to do "install an unrelated app", and nobody could see
why a rain service needed it. Web push on Android needs no install at all.

Three things are worth knowing before changing anything here.

**We do the crypto, not an HTTP library.** `pywebpush` is the usual choice and was rejected: it
brings `requests` *and* `aiohttp`, neither of which this project uses, and it owns the HTTP call -
so the injectable `httpx.BaseTransport` that every other transport here is tested through would
not work. What is left after dropping it is small: `py_vapid` signs the header, `http_ece` encrypts
the body, `httpx` posts it. Both of those libraries are what pywebpush itself uses.

**The endpoint is a URL a stranger can choose.** It arrives in a subscribe request, and this module
POSTs to it. Unchecked, that is a server-side request forgery primitive pointing at the metadata
service, the database, or anything else this container can reach. Hence `ALLOWED_PUSH_HOSTS` and
`check_endpoint`, enforced here *and* at subscribe time - here because this is the code that makes
the request, and there because a row that should never exist is better never stored.

**404 and 410 mean delete, not retry.** They are the only notification we ever get that somebody
unsubscribed by blocking notifications or clearing their browser data. `DeliveryResult.gone` carries
that, and the caller deletes the subscriber.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from urllib.parse import urlparse

import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from rainalert.notify.base import DeliveryResult, OutboundMessage

#: What the Notification API actually renders. `Notification.maxActions` is 2 on every shipping
#: Chromium and Firefox build, and anything past that index is discarded silently at display time -
#: a button that exists in the payload, is never drawn, and is therefore a feature that looks
#: present and is not. ntfy allowed three; this is one fewer, and nothing here sends more than two.
MAX_ACTIONS = 2

#: The hosts a push endpoint may point at. Not a nicety: `send` POSTs to whatever endpoint it is
#: given, so without this an attacker can subscribe with
#: `http://169.254.169.254/computeMetadata/v1/...` and have this service fetch it for them.
#: Subdomains are matched at any depth, so `updates.push.services.mozilla.com` is allowed by
#: `push.services.mozilla.com` while `push.services.mozilla.com.evil.test` is not - the leading dot
#: in the suffix test is what closes that. (This said "one level deep", which was wrong: nothing
#: counts the labels. Any depth is fine, because every label below an allowed host belongs to its
#: operator.)
ALLOWED_PUSH_HOSTS = (
    # Chrome, Edge and every other Chromium build.
    "fcm.googleapis.com",
    "android.googleapis.com",
    "gcm-http.googleapis.com",
    # Firefox, whose endpoint host has a subdomain.
    "push.services.mozilla.com",
    # Safari, including iOS home-screen web apps.
    "web.push.apple.com",
    # Edge's older WNS-backed endpoints, still issued to some installs.
    "notify.windows.com",
    "push.services.microsoft.com",
)


#: RFC 1123 host syntax, lowercase because `urlparse` already lowercases `.hostname`. Labels are
#: not length-checked: the allowlist decides which hosts are acceptable, this only decides what
#: counts as a hostname at all.
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")

#: Google's sharded GCM push hosts: `jmt17.google.com`, `jmt42.google.com`, and so on.
#:
#: These are not in `ALLOWED_PUSH_HOSTS` because the shard number varies per subscription, so no
#: fixed list can cover them. A real Chrome install handed out `jmt17.google.com` and this service
#: refused it - every Chrome subscriber on a `jmt*` shard could complete the browser half of a
#: signup, see the subscription appear in their browser's own settings, and be rejected by us with
#: "check your input". The allowlist was written from what the documentation says Chrome uses
#: rather than from what Chrome was observed to emit, and the two differ.
#:
#: Anchored on both ends and requiring digits, so this admits the shard family and nothing else:
#: `jmt17.google.com` yes, `jmt17.google.com.evil.test` no, `notjmt17.google.com` no,
#: `jmt17.google.com` as a *suffix* of a longer host no.
_GOOGLE_SHARD = re.compile(r"jmt[0-9]+\.google\.com")


class EndpointRefused(ValueError):
    """The endpoint is not a push service we will talk to."""


def check_endpoint(endpoint: str, allowed_hosts: tuple[str, ...] = ALLOWED_PUSH_HOSTS) -> str:
    """Return `endpoint` if it is an https URL at a known push service, else raise.

    Called at subscribe time and again before every send. Twice on purpose: the first stops the row
    existing, the second means a row that got in by some other route - a migration, a fixture, a
    hand-written INSERT - still cannot turn into an outbound request to anywhere it likes.
    """
    # Every value read off the URL is read here, inside one guard. `urlparse` and its `.port` and
    # `.hostname` properties each raise a bare `ValueError` on input a caller controls, and this
    # function's whole contract is that it returns or raises `EndpointRefused` - a bare ValueError
    # escapes both `subscribe` (which catches EndpointRefused) and the route (which catches
    # ValidationError), so it surfaced as an unauthenticated 500 from a public endpoint.
    #
    # An earlier fix guarded `.port` alone, having found that case, and left the other two. Review
    # then found them: `https://[fcm.googleapis.com]/x` raises from `.hostname` ("does not appear to
    # be an IPv4 or IPv6 address", because brackets mean an IP literal), and a netloc containing a
    # character that NFKC-normalises to include one of `/?#@:` - `https://℀.fcm.googleapis.com/x`,
    # where U+2100 expands to "a/c" - raises from `urlparse` itself. Guarding the parse as a whole
    # rather than each accessor is what stops this recurring: there is no list of raising properties
    # to keep up to date.
    # Printable ASCII only, checked before anything is parsed. A URL has no business carrying a
    # control character, and three separate things downstream disagree about what to do with one:
    # `OutboundMessage.__post_init__` raises a bare ValueError on CR or LF (header injection), httpx
    # raises `InvalidURL` on NUL or TAB, and `urlparse` is happy with all four. So an endpoint with
    # a newline in it passed this function, passed `subscribe`, was *committed*, and then blew up
    # while the confirmation message was being built - an unauthenticated 500 that left behind a row
    # holding somebody's coordinates that can never be confirmed (no message can be built for it),
    # is excluded from `due_for_liveness` (which requires `confirmed_at`), and is therefore deleted
    # by nothing, ever. A location retained forever for a signup that never completed.
    #
    # One gate here rather than three fixes downstream: this is the function whose job is deciding
    # whether we will talk to a URL at all.
    if any(ch < "\x21" or ch > "\x7e" for ch in endpoint):
        raise EndpointRefused("a push endpoint must be printable ASCII")
    try:
        parsed = urlparse(endpoint)
        scheme = parsed.scheme
        has_userinfo = parsed.username is not None or parsed.password is not None
        port = parsed.port
        host = parsed.hostname or ""
    except ValueError as exc:
        # Deliberately not echoing `exc`: its message quotes the netloc, and this string reaches the
        # logs on every refusal.
        raise EndpointRefused("a push endpoint must be a well-formed https URL") from exc

    if scheme != "https":
        raise EndpointRefused("a push endpoint must be https")
    # Userinfo, rejected outright. `https://evil.test@fcm.googleapis.com/...` passes a hostname
    # check - urlparse reports the right host - and then httpx turns the userinfo into an
    # `Authorization: Basic ...` header that *replaces* ours, so the push service sees no VAPID at
    # all and refuses every send. With a colon in it, the derived `aud` is malformed and py_vapid
    # raises from outside the try. Only self-harm, but it produces a row that can never be
    # delivered to, and DESIGN.md claimed this form was already refused. It was not.
    if has_userinfo:
        raise EndpointRefused("a push endpoint must not carry userinfo")
    # The host allowlist alone leaves the port free, so `https://fcm.googleapis.com:22/x` passed.
    # Not an internal SSRF - the host is still a real push service - but it lets an unauthenticated
    # caller make this service open TLS connections to arbitrary ports on a third party's address,
    # and a blackholed port costs the full 10 s timeout on every send attempt for that subscriber.
    # No push service publishes an endpoint on another port.
    if port not in (None, 443):
        raise EndpointRefused("a push endpoint must be on port 443")
    # A hostname is letters, digits, dots and hyphens (RFC 1123), and nothing else. Without this,
    # `https://169.254.169.254\.fcm.googleapis.com/x` satisfied the allowlist below - the label
    # `169.254.169.254\` ends with `.fcm.googleapis.com`, so the suffix match was happy. Review
    # confirmed it is not an SSRF today: `urlparse` and httpx agree on that host string and a name
    # containing a backslash does not resolve, so the request fails rather than going somewhere. It
    # is still a syntactically impossible host that a check whose job is naming exactly six services
    # accepted, and the next reader of `host.endswith(...)` should not have to work out why that is
    # safe.
    if host and not _HOSTNAME.fullmatch(host):
        raise EndpointRefused("a push endpoint host must be a plain hostname")
    for allowed in allowed_hosts:
        # Exact, or a subdomain of it. The dot is part of the suffix so that a host merely *ending*
        # in the allowed string - "notfcm.googleapis.com" - does not match.
        if host == allowed or host.endswith(f".{allowed}"):
            return endpoint
    if _GOOGLE_SHARD.fullmatch(host):
        return endpoint
    # Named in the message because this is the one refusal a *legitimate* browser can trigger, and
    # when it happens the host is the entire diagnosis. `subscribe` logs this.
    raise EndpointRefused(f"{host!r} is not a known push service")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unb64url(text: str) -> bytes:
    """Decode base64url that a browser sent without padding.

    `PushSubscription.getKey()` values are unpadded, and Python's decoder insists on padding, so
    it has to be put back. A wrong length raises `binascii.Error`, which callers turn into a
    refusal rather than a 500.
    """
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


#: The most plaintext that fits one aes128gcm record inside the 4096 octets RFC 8030 obliges a push
#: service to accept: 4096 less the 86-octet content-encoding header and 17 octets of record
#: framing. RFC 8291 requires a single record, so exceeding this is not merely large - http_ece
#: silently splits it, producing a body a minimal receiver may refuse and FCM answers 413 for.
MAX_PAYLOAD_BYTES = 3993

#: RFC 8292 caps `exp` at 24 hours and the push service checks it against its own clock, so sitting
#: on the ceiling turns any forward skew into total non-delivery. Half of it, as the other libraries
#: do.
VAPID_TOKEN_LIFETIME_SECONDS = 12 * 3600


def payload_for(message: OutboundMessage) -> bytes:
    """The JSON the service worker receives and turns into a notification.

    Shaped for `static/sw.js`: it reads `title`, `body`, `url` and `actions`, and keeps the whole
    object in the notification's `data` so `notificationclick` can find the action it was given.
    The action's id is its index, which is why order matters and why the array is truncated rather
    than reordered.
    """
    if len(message.actions) > MAX_ACTIONS:
        raise ValueError(
            f"a notification renders at most {MAX_ACTIONS} actions, got {len(message.actions)}"
        )
    body = json.dumps(
        {
            "title": message.subject,
            "body": message.text,
            "url": message.click_url or "",
            # Omitted when unset so the worker's own default applies, rather than sending null and
            # making sw.js decide what null means.
            **({"tag": message.push_tag} if message.push_tag else {}),
            "actions": [
                {
                    "title": action.label,
                    "url": action.url,
                    "body": action.body,
                    "contentType": action.content_type,
                }
                for action in message.actions
            ],
        },
        ensure_ascii=False,
    ).encode()
    if len(body) > MAX_PAYLOAD_BYTES:
        # Refused rather than truncated: a notification cut off mid-sentence is worse than one that
        # fails loudly here, and every string in it is ours, so this is a bug in message building.
        raise ValueError(
            f"a push payload may be at most {MAX_PAYLOAD_BYTES} bytes, got {len(body)}"
        )
    return body


class WebPushNotifier:
    """Posts an encrypted notification to one browser's push endpoint."""

    def __init__(
        self,
        *,
        vapid_private_key: str,
        vapid_subject: str,
        ttl_seconds: int = 1800,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        allowed_hosts: tuple[str, ...] = ALLOWED_PUSH_HOSTS,
    ) -> None:
        if not vapid_private_key:
            raise ValueError("web push needs a VAPID private key")
        if not vapid_subject.startswith(("mailto:", "https://")):
            # RFC 8292 says the `sub` claim is a contact the push service can use to reach the
            # operator. A malformed one is accepted by some services and rejected by others, which
            # is the worst outcome: it works in testing and fails for a subset of subscribers.
            raise ValueError("vapid_subject must be a mailto: or https: URL")
        self._vapid = Vapid02.from_pem(vapid_private_key.encode())
        self._subject = vapid_subject
        # RFC 8030: how long the push service should hold the message for a device that is offline.
        # Kept equal to `dispatcher.MAX_NOTIFICATION_AGE`, which is the same judgement expressed on
        # our side of the wire - a warning we would refuse to send as stale is one we should not ask
        # anyone else to hold either.
        self._ttl = ttl_seconds
        self._allowed_hosts = allowed_hosts
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)

    @property
    def application_server_key(self) -> str:
        """The public key the browser needs, base64url - `applicationServerKey` on subscribe.

        Derived from the private key rather than configured separately, so the two cannot drift.
        A mismatch is not a visible error: the browser subscribes fine and every later send is
        rejected by the push service as unauthorised.
        """
        return b64url(
            self._vapid.public_key.public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )

    def send(self, message: OutboundMessage) -> DeliveryResult:
        if not (message.push_p256dh and message.push_auth):
            return DeliveryResult(ok=False, error="web push needs p256dh and auth")
        try:
            endpoint = check_endpoint(message.to, self._allowed_hosts)
        except EndpointRefused as exc:
            return DeliveryResult(ok=False, error=str(exc))

        # Built *outside* the try below, and that placement is the whole point. `payload_for` raises
        # for too many actions or an over-long body - our mistakes, not the subscriber's - and the
        # handler below reports `gone`, which makes `deliver_queued` delete the subscriber row and
        # cascade away their coordinates and alert state. A bug in our own message building must not
        # silently delete the people it affects, least of all when D-47 means they cannot be
        # restored.
        try:
            plaintext = payload_for(message)
        except ValueError as exc:
            return DeliveryResult(ok=False, error=f"cannot build this payload: {exc}")

        try:
            body = self._encrypt(plaintext, message.push_p256dh, message.push_auth)
        except Exception as exc:  # noqa: BLE001 - a bad key is a dead row, not a crash
            # Malformed keys come from a browser we cannot re-ask, so this row will fail forever.
            # Reported as gone so the caller deletes it rather than retrying every five minutes.
            return DeliveryResult(
                ok=False, error=f"cannot encrypt for this subscriber: {exc}", gone=True
            )

        parsed = urlparse(endpoint)
        headers = self._vapid.sign(
            {
                # An RFC 6454 origin, so no default port: `netloc` keeps an explicit `:443`, and a
                # push service comparing the string rejects `https://fcm.googleapis.com:443`.
                "aud": f"{parsed.scheme}://{parsed.hostname}"
                + (f":{parsed.port}" if parsed.port not in (None, 443) else ""),
                "sub": self._subject,
                # Set here rather than left to py_vapid, which fills in exactly 86400 seconds -
                # RFC 8292's hard ceiling. A push service validates `exp` against *its* clock, so
                # any positive skew on our side makes every token "more than 24 hours" out and the
                # push service answers 401 (autopush: "Auth expired") or 403. Neither is a 410, so
                # nothing is deleted and nothing is logged as unusual: delivery simply stops for
                # everyone until the clock is fixed. A fresh JWT is signed for every send, so a long
                # expiry buys nothing; twelve hours is what pywebpush and node web-push default to.
                "exp": int(time.time()) + VAPID_TOKEN_LIFETIME_SECONDS,
            }
        )
        headers.update(
            {
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                # The message may override both. See `OutboundMessage.ttl_seconds` for why one
                # sender-wide number was wrong: it is right for a rain warning and throws away a
                # confirmation whose token is good for a day.
                "TTL": str(message.ttl_seconds if message.ttl_seconds is not None else self._ttl),
                # RFC 8030 §5.3. A rain warning is worth waking a dozing phone for; the monthly
                # liveness ping is not.
                "Urgency": message.urgency or "high",
            }
        )
        # No Crypto-Key header: aes128gcm carries our ephemeral public key inside the body's own
        # header block. Sending one as well is the draft-01 shape and some services reject it.

        try:
            response = self._client.post(endpoint, content=body, headers=headers)
        # `InvalidURL` is deliberately named alongside `HTTPError`: it does NOT subclass it (it comes
        # off `Exception` directly), so it escaped a method whose entire contract - relied on by
        # `RoutingNotifier`, which passes the return value straight through, and by
        # `deliver_queued`, which branches on it - is that it returns a `DeliveryResult` and never
        # raises. httpx raises it for a NUL or TAB in the URL, which `check_endpoint` now refuses
        # before a row can exist; this is the second lock on the same door, because the callers'
        # broad `except Exception` is what stops today's escape from crashing a delivery run and
        # that is luck rather than design.
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            return DeliveryResult(ok=False, error=f"push request failed: {exc}")

        if response.status_code in (404, 410):
            return DeliveryResult(
                ok=False, error=f"subscription gone ({response.status_code})", gone=True
            )
        if response.status_code >= 400:
            # Truncated: a push service's error body is occasionally an HTML page, and the whole of
            # it would end up in `notifications.error` and in the logs.
            return DeliveryResult(
                ok=False, error=f"push service said {response.status_code}: {response.text[:200]}"
            )
        # 201 is the documented success; some services answer 200 or 202.
        return DeliveryResult(ok=True, provider_message_id=response.headers.get("location"))

    def _encrypt(self, plaintext: bytes, p256dh: str, auth: str) -> bytes:
        """RFC 8291 aes128gcm, to this subscriber's keys.

        A fresh ephemeral key per message, which is what the RFC requires - reusing one would let
        the push service link two messages to the same sender key.
        """
        return http_ece.encrypt(
            plaintext,
            private_key=ec.generate_private_key(ec.SECP256R1()),
            dh=unb64url(p256dh),
            auth_secret=unb64url(auth),
            version="aes128gcm",
        )

    def close(self) -> None:
        self._client.close()


def generate_vapid_keys() -> tuple[str, str]:
    """A new VAPID keypair: (private key PEM, public key base64url).

    Used by `rainalert vapid-keys` to produce what goes into Secret Manager and what the page hands
    the browser. Rotating this key invalidates **every existing subscription** - the push services
    reject a signature from a key the subscription was not created with - so it is generated once
    per deployment and treated like `SECRET_KEY`.
    """
    vapid = Vapid02()
    vapid.generate_keys()
    private_pem = vapid.private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = b64url(
        vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    return private_pem, public


def new_auth_secret() -> str:
    """A 16-byte secret, base64url. Only used by tests that stand in for a browser."""
    return b64url(os.urandom(16))


__all__ = [
    "ALLOWED_PUSH_HOSTS",
    "MAX_ACTIONS",
    "EndpointRefused",
    "WebPushNotifier",
    "check_endpoint",
    "generate_vapid_keys",
    "payload_for",
]
