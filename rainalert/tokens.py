"""Opaque tokens: confirmation, unsubscribe, and the long-lived API bearer.

Rules, all of which exist because of how these tokens travel:

* The **plaintext is never stored** - only ``sha256`` of it. A token is generated once, put into
  exactly one email or one response body, and is thereafter only ever compared by hash.
* Comparison is by hash lookup, so it is inherently constant-time with respect to the secret.
* Confirmation tokens are **single use and expire**; API tokens are long-lived and revocable.
* A token must never travel in a URL that ends up in a log. Cloud Run logs full request URLs
  including the query string, so the confirm and unsubscribe flows take the token from a POST body
  (SECURITY_REVIEW.md F-4, F-8).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: 32 bytes of urlsafe base64 - ~43 characters, 256 bits of entropy. Not guessable, and short
#: enough to paste.
TOKEN_BYTES = 32


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def hash_address(channel: str, address: str) -> bytes:
    """Stable lookup key for a subscriber, covering the channel as well as the address.

    Without the channel an ntfy topic spelled like a mailbox would collide with that mailbox and
    the two subscribers would be one.

    Mailboxes are case-folded because they are case-insensitive in practice. Topics are not:
    ntfy treats `Abc` and `abc` as different topics, and folding them would make two distinct
    push destinations look like one subscriber.
    """
    normalised = address.strip().lower() if channel == "email" else address.strip()
    return hashlib.sha256(f"{channel}:{normalised}".encode()).digest()


def hash_ip(ip: str, secret: str) -> bytes:
    """Salted hash of a client IP, for the consent record and rate limiting.

    An unsalted hash of an IPv4 address is not anonymous: the whole space is 2^32 and a laptop
    enumerates it in minutes. The salt is the application secret, so the hashes are useless to
    anyone who does not also have it.
    """
    return hmac.new(secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).digest()


def expiry(hours: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(hours=hours)


def unsubscribe_token(subscriber_id: uuid.UUID, secret: str) -> str:
    """A signed, stateless unsubscribe token.

    Every alert mail must carry a working unsubscribe link, but stored tokens are stored as hashes
    only - so a link cannot be rebuilt from the database later. Signing solves that without ever
    persisting a secret: the token is the subscriber id plus an HMAC over it, verifiable at any
    time, and invalidated wholesale by rotating SECRET_KEY.
    """
    mac = hmac.new(secret.encode("utf-8"), str(subscriber_id).encode("ascii"), hashlib.sha256)
    return f"{subscriber_id}.{base64.urlsafe_b64encode(mac.digest()).decode().rstrip('=')}"


def verify_unsubscribe_token(token: str, secret: str) -> uuid.UUID | None:
    """Return the subscriber id, or None. Constant-time comparison."""
    raw_id, _, signature = token.partition(".")
    if not signature:
        return None
    try:
        subscriber_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    expected = unsubscribe_token(subscriber_id, secret).partition(".")[2]
    if not hmac.compare_digest(signature, expected):
        return None
    return subscriber_id


@dataclass(frozen=True)
class SignedToken:
    """What a verified session or CSRF token says about itself."""

    subscriber_id: uuid.UUID
    #: When this token stops working.
    expires: int
    #: The wall beyond which the *session* may not be extended, carried so that renewing does
    #: not need a session table to remember when the session began.
    deadline: int

    def seconds_left(self, now: datetime | None = None) -> int:
        return max(0, self.expires - int((now or datetime.now(UTC)).timestamp()))

    def seconds_until_deadline(self, now: datetime | None = None) -> int:
        return max(0, self.deadline - int((now or datetime.now(UTC)).timestamp()))


def _sign(purpose: str, subscriber_id: uuid.UUID, expires: int, deadline: int, secret: str) -> str:
    """`purpose.id.expiry.deadline.mac`, with everything before the mac inside the mac.

    The purpose is signed, not merely prefixed: without it a session cookie and a CSRF token -
    same id, same expiry, same secret - would have identical signatures, and the one that is
    readable by the page would be usable as the one that is not.

    The deadline is signed for the same reason the expiry is. It is the only record of when a
    session started, so a holder who could edit it could renew forever.
    """
    payload = f"{purpose}:{subscriber_id}:{expires}:{deadline}"
    mac = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256)
    signature = base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")
    return f"{purpose}.{subscriber_id}.{expires}.{deadline}.{signature}"


def _verify(
    purpose: str, token: str, secret: str, now: datetime | None = None
) -> SignedToken | None:
    """Return the token's claims if it is well formed, unexpired and ours."""
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != purpose:
        return None
    _, raw_id, raw_expiry, raw_deadline, signature = parts
    try:
        subscriber_id = uuid.UUID(raw_id)
        expires = int(raw_expiry)
        deadline = int(raw_deadline)
    except ValueError:
        return None
    expected = _sign(purpose, subscriber_id, expires, deadline, secret).rpartition(".")[2]
    # Signature first: an expired token and a forged one should cost the same to probe.
    if not hmac.compare_digest(signature, expected):
        return None
    if (now or datetime.now(UTC)).timestamp() >= expires:
        return None
    return SignedToken(subscriber_id, expires, deadline)


def session_token(
    subscriber_id: uuid.UUID,
    secret: str,
    ttl_minutes: int,
    deadline: int | None = None,
    now: datetime | None = None,
) -> str:
    """The settings-page session, carried in a cookie.

    Stateless and signed rather than a row, for the same reason as the unsubscribe token: there
    is no session table and inventing one to hold thirty minutes of state is more machinery than
    the problem deserves. Rotating SECRET_KEY ends every session at once, which is the only
    revocation this needs.

    ``deadline`` is carried forward when a session is renewed, so the wall stays where the first
    one put it. Renewal past it is refused; that is what stops a renew button turning a
    deliberately short session into a permanent one.
    """
    at = now or datetime.now(UTC)
    expires = int((at + timedelta(minutes=ttl_minutes)).timestamp())
    if deadline is not None:
        expires = min(expires, deadline)
    return _sign("session", subscriber_id, expires, deadline if deadline else expires, secret)


def verify_session_token(token: str, secret: str, now=None) -> SignedToken | None:
    return _verify("session", token, secret, now)


def csrf_token(
    subscriber_id: uuid.UUID, secret: str, expires: int, now: datetime | None = None
) -> str:
    """Rendered into the settings page and echoed back in a header on every write.

    The session cookie alone is not enough. `SameSite=Lax` blocks a cross-site form POST, but
    it is one browser default away from being the only thing standing there - so the write also
    requires a value that can only be obtained by *reading* the page, which the same-origin
    policy denies to another site (SECURITY_REVIEW.md F-16).

    Takes an absolute expiry rather than a lifetime, because it must be **the session's** expiry.
    Given its own clock it drifts: every page load minted a fresh thirty minutes while the
    session's own expiry stayed put, so a CSRF token could outlive the session it belongs to.
    Harmless - the session is checked first - but two things that are meant to be one.
    """
    del now  # kept for call-site symmetry; the expiry is absolute
    return _sign("csrf", subscriber_id, expires, expires, secret)


def verify_csrf_token(token: str, secret: str, now=None) -> SignedToken | None:
    return _verify("csrf", token, secret, now)


def locate_token(
    subscriber_id: uuid.UUID, secret: str, ttl_minutes: int, now: datetime | None = None
) -> str:
    """Lets the map show the place a warning was about, for as long as the warning is about it.

    The obvious shape - putting the coordinates in the link - was rejected: the warning sits in
    a notification list for good, and a screenshot of one would then be somebody's home address
    in plain text. It is also a step backwards from what the message says today, which names a
    time and an intensity but never a place.

    So the link carries a signed reference instead, and the coordinates are fetched. Once it
    expires the map opens where it always did, which is the point: a tap on last week's warning
    should tell a reader nothing about where its owner lives.
    """
    at = now or datetime.now(UTC)
    expires = int((at + timedelta(minutes=ttl_minutes)).timestamp())
    return _sign("locate", subscriber_id, expires, expires, secret)


def verify_locate_token(token: str, secret: str, now=None) -> SignedToken | None:
    return _verify("locate", token, secret, now)


def manage_request_token(
    subscriber_id: uuid.UUID, secret: str, ttl_days: int, now: datetime | None = None
) -> str:
    """The durable token that rides in a notification and can *ask* for a settings link.

    It is deliberately the weakest credential in the system. Everything else that reaches the
    settings page is short-lived precisely because it opens someone's home coordinates; this one
    is long-lived because it has to survive in a notification the reader is told to keep, and it
    can survive there safely only because holding it grants nothing except "send the real link
    to the channel that already received this message".

    So a forwarded screenshot of an alert is not a key. Whoever can read the notification can
    read the topic, and whoever can read the topic could already ask for a link through the
    settings form - the token adds convenience, not reach.

    Signed rather than stored for the same reason as the unsubscribe token: the plaintext is
    never kept, so every future alert can mint a working one from the id alone (see the module
    docstring). Rotating SECRET_KEY invalidates them all; deleting the subscriber makes them
    resolve to nobody, which is the revocation that matters.
    """
    at = now or datetime.now(UTC)
    expires = int((at + timedelta(days=ttl_days)).timestamp())
    return _sign("request", subscriber_id, expires, expires, secret)


def verify_manage_request_token(token: str, secret: str, now=None) -> SignedToken | None:
    return _verify("request", token, secret, now)
