"""Device keys: how a push subscriber's browser proves itself on every settings request (D-64).

The browser holds a non-extractable ECDSA P-256 key in IndexedDB and signs each request to the
settings API; we hold the public half (`DeviceKey`). There is no session for these requests - no
cookie, no CSRF value, nothing to expire - because a signature over the request itself is cheaper
to check than a session is to manage, and nothing a browser attaches on its own can carry one
(docs/PLAN_DEVICE_KEY.md §4.2, §9).

What is signed, newline-separated:

    rainalert-request-v1
    <origin, from public_base_url - never from the request>
    <METHOD>
    <path exactly as received (the raw, still percent-encoded path)>
    <t: unix seconds on the server's clock, as the browser computed it>
    <base64url(SHA-256(the body bytes received))>

No field can contain a newline, so the message is unambiguous. The origin comes from configuration
because behind Firebase Hosting the app sees the Cloud Run hostname over plain http
(PLAN_DEVICE_KEY.md §11); built from the request, every signature would fail.

Everything here is pure: parsing, building and verifying. The routes and the database live in
`api/app.py` and `subscriptions.py`.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

#: Bumped if the signed message ever changes shape, so an old signature cannot be read as a new one.
MESSAGE_PREFIX = "rainalert-request-v1"
#: How far the `t` in a signature may be from the server's clock, either way. The page signs with
#: server time it has synchronised, not its own clock (§4.3), so this covers network latency and a
#: phone waking from sleep, not drifting clocks. Also the replay window (§4.4).
CLOCK_WINDOW_SECONDS = 120
#: A P-256 SubjectPublicKeyInfo is 91 octets. Anything much longer is not one of ours.
MAX_SPKI_BYTES = 128
#: The `client` value the current pages send with a redemption. A redemption without it comes from
#: a page that was open before the device-key release and is handled as before (no key, cookie
#: session) - see `subscriptions.PushProof`.
CLIENT_VERSION = "dk1"

_HEADER = re.compile(
    r"RainKey key=(?P<key>[A-Za-z0-9_-]{43}), t=(?P<t>[0-9]{1,12}), sig=(?P<sig>[A-Za-z0-9_-]{86})"
)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    """Strict base64url without padding. Raises ValueError on anything else."""
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text or ""):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key_id_for(spki: bytes) -> str:
    """`base64url(SHA-256(SPKI))`: 43 characters, computed the same way by the browser."""
    return b64url(hashlib.sha256(spki).digest())


def parse_public_key(encoded: str) -> bytes:
    """The SPKI the browser sent, if it is an uncompressed P-256 key in canonical DER.

    Returned unchanged, because the key id is a hash of exactly these bytes. Re-serialised and
    compared, so a non-canonical encoding of the same key - which would hash to a different id -
    is refused rather than stored under an id the browser cannot compute.
    """
    try:
        raw = unb64url(encoded)
    except ValueError as exc:
        raise ValueError("device key is not base64url") from exc
    if not raw or len(raw) > MAX_SPKI_BYTES:
        raise ValueError("device key has the wrong length")
    try:
        key = serialization.load_der_public_key(raw)
    except ValueError as exc:
        raise ValueError("device key is not a public key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        # ValueError, not TypeError: to the caller every unusable key is the same bad input.
        raise ValueError("device key is not a P-256 key")  # noqa: TRY004
    canonical = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if canonical != raw:
        raise ValueError("device key is not in canonical form")
    return raw


def site_origin(public_base_url: str) -> str:
    """`scheme://host[:port]` the way a browser's `location.origin` spells it."""
    parts = urlsplit(public_base_url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    default = {"http": 80, "https": 443}.get(scheme)
    netloc = host if port in (None, default) else f"{host}:{port}"
    return f"{scheme}://{netloc}"


@dataclass(frozen=True)
class SignedHeader:
    key_id: str
    t: int
    signature: bytes


def parse_header(value: str) -> SignedHeader | None:
    """The `Authorization: RainKey ...` header, or None if it is not exactly that shape."""
    match = _HEADER.fullmatch(value or "")
    if match is None:
        return None
    return SignedHeader(match["key"], int(match["t"]), unb64url(match["sig"]))


def message(origin: str, method: str, raw_path: str, t: int, body: bytes) -> bytes:
    body_hash = b64url(hashlib.sha256(body).digest())
    return "\n".join((MESSAGE_PREFIX, origin, method.upper(), raw_path, str(t), body_hash)).encode()


def verify(spki: bytes, signed: bytes, signature: bytes) -> bool:
    """ECDSA P-256 / SHA-256 over `signed`, with WebCrypto's raw `r||s` signature.

    WebCrypto produces IEEE P1363 (64 octets); `cryptography` wants DER, hence the conversion.
    Anything that is not exactly 64 octets is refused before it gets there. Malleability does not
    matter: a mutated signature authorises the same request.
    """
    if len(signature) != 64:
        return False
    key = serialization.load_der_public_key(spki)
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    try:
        key.verify(encode_dss_signature(r, s), signed, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


def within_window(t: int, now: float) -> bool:
    return abs(now - t) <= CLOCK_WINDOW_SECONDS
