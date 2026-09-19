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
