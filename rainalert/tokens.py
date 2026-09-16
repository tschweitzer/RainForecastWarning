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

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

#: 32 bytes of urlsafe base64 - ~43 characters, 256 bits of entropy. Not guessable, and short
#: enough to paste.
TOKEN_BYTES = 32


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def hash_email(email: str) -> bytes:
    """Stable lookup key for an address. Case-folded, because mailboxes are."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).digest()


def hash_ip(ip: str, secret: str) -> bytes:
    """Salted hash of a client IP, for the consent record and rate limiting.

    An unsalted hash of an IPv4 address is not anonymous: the whole space is 2^32 and a laptop
    enumerates it in minutes. The salt is the application secret, so the hashes are useless to
    anyone who does not also have it.
    """
    return hmac.new(secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).digest()


def expiry(hours: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(hours=hours)
