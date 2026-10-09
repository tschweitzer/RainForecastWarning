"""Device keys: the settings page without sessions or push round trips (DESIGN.md D-64).

docs/PLAN_DEVICE_KEY.md is the design and its three security reviews; the section numbers below
refer to it. The browser is played by `Browser`, which signs exactly as `static/devicekey.js` does:
ECDSA P-256 over `devicekeys.message`, with WebCrypto's raw r||s signature.
"""

from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient
from sqlalchemy import select

from rainalert import devicekeys
from rainalert.api.app import MANAGE_COOKIE, create_app
from rainalert.config import Settings
from rainalert.db.models import AuthToken, DeviceKey, Subscriber, TokenPurpose
from rainalert.notify import ConsoleNotifier

SITE = "https://rain.example.invalid"
MUNICH = (48.1533, 11.5574)
ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-key-test"
P256DH = "k" * 87


class Browser:
    """A browser holding a device key, signing the way devicekey.js does."""

    def __init__(self, curve: ec.EllipticCurve | None = None):
        self.key = ec.generate_private_key(curve or ec.SECP256R1())
        self.spki = self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    @property
    def public(self) -> str:
        return devicekeys.b64url(self.spki)

    @property
    def key_id(self) -> str:
        return devicekeys.key_id_for(self.spki)

    def sign(self, signed: bytes) -> bytes:
        r, s = decode_dss_signature(self.key.sign(signed, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def header(self, method: str, path: str, body: bytes = b"", t: int | None = None) -> str:
        t = int(time.time()) if t is None else t
        sig = self.sign(devicekeys.message(SITE, method, path, t, body))
        return f"RainKey key={self.key_id}, t={t}, sig={devicekeys.b64url(sig)}"

    def request(self, client, method: str, path: str, body: bytes = b"", **kwargs):
        headers = {"Authorization": self.header(method, path, body, **kwargs)}
        if body:
            headers["Content-Type"] = "application/json"
        return client.request(method, path, content=body, headers=headers)


@pytest.fixture()
def settings():
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url=SITE,
        mail_from="RainAlert <noreply@rain.example.invalid>",
        secret_key="test-secret",
        notifier="console",
        _env_file=None,
    )


@pytest.fixture()
def notifier():
    return ConsoleNotifier()


def make_client(db, settings, notifier):
    return TestClient(
        create_app(settings, session_factory=db, notifier=notifier),
        base_url=settings.public_base_url,
    )


@pytest.fixture()
def client(db, settings, notifier):
    return make_client(db, settings, notifier)


def subscribe_push(client, notifier, endpoint=ENDPOINT) -> str:
    """Signs up a push subscriber and returns the confirmation token, unspent."""
    client.post(
        "/api/v1/subscriptions",
        json={
            "channel": "webpush",
            "lat": MUNICH[0],
            "lon": MUNICH[1],
            "endpoint": endpoint,
            "p256dh": P256DH,
            "auth": "a" * 22,
        },
    )
    return notifier.sent[-1].click_url.split("/confirm#")[1].split("=", 1)[1]


def confirm(client, token, browser=None, endpoint=ENDPOINT, p256dh=P256DH, client_version="dk1"):
    data = {"token": token}
    if client_version:
        data.update(client=client_version, endpoint=endpoint, p256dh=p256dh)
        if browser:
            data["device_key"] = browser.public
    return client.post("/confirm", data=data)


def enrolled(client, notifier) -> Browser:
    """A confirmed push subscriber whose browser holds a device key - and no session."""
    browser = Browser()
    response = confirm(client, subscribe_push(client, notifier), browser)
    assert response.status_code == 200
    assert f'data-key-enrolled="{browser.key_id}"' in response.text
    assert MANAGE_COOKIE not in response.cookies, "no session when a key was registered"
    client.cookies.clear()
    return browser


def link_token(notifier) -> str:
    return notifier.sent[-1].click_url.split("/manage#t=")[1]


# --- the pure parts --------------------------------------------------------------------------


def test_a_webcrypto_style_key_parses_and_its_id_is_a_hash_of_it():
    browser = Browser()
    assert devicekeys.parse_public_key(browser.public) == browser.spki
    assert len(browser.key_id) == 43


@pytest.mark.parametrize("bad", ["", "not base64!", "QUJD", "=" * 4])
def test_garbage_is_not_a_key(bad):
    with pytest.raises(ValueError):
        devicekeys.parse_public_key(bad)


def test_only_p256_is_accepted():
    with pytest.raises(ValueError, match="P-256"):
        devicekeys.parse_public_key(Browser(ec.SECP384R1()).public)


def test_a_compressed_encoding_of_a_good_key_is_refused():
    """Same key, different bytes - and so a different id than the browser computes."""
    key = Browser().key.public_key()
    compressed = key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    )
    # SPKI header for an EC P-256 key, followed by the compressed point as the BIT STRING.
    algorithm = bytes.fromhex("301306072a8648ce3d020106082a8648ce3d030107")
    bitstring = b"\x03" + bytes([len(compressed) + 1]) + b"\x00" + compressed
    body = algorithm + bitstring
    spki = b"\x30" + bytes([len(body)]) + body
    with pytest.raises(ValueError):
        devicekeys.parse_public_key(devicekeys.b64url(spki))


def test_a_signature_verifies_and_every_field_is_covered():
    browser = Browser()
    base = (SITE, "PATCH", "/api/v1/subscriptions/me", 1000, b'{"radius_m":500}')
    sig = browser.sign(devicekeys.message(*base))
    assert devicekeys.verify(browser.spki, devicekeys.message(*base), sig)
    for index, changed in enumerate(
        ("https://evil.example", "PUT", "/api/v1/subscriptions/me/location", 1001, b"{}")
    ):
        fields = list(base)
        fields[index] = changed
        assert not devicekeys.verify(browser.spki, devicekeys.message(*fields), sig), index
    assert not devicekeys.verify(Browser().spki, devicekeys.message(*base), sig), "other key"
    assert not devicekeys.verify(browser.spki, devicekeys.message(*base), sig[:63]), "length"


@pytest.mark.parametrize(
    "value",
    [
        "Bearer x",
        "RainKey key=short, t=1, sig=" + "A" * 86,
        "RainKey key=" + "A" * 43 + ", t=-1, sig=" + "A" * 86,
        "RainKey key=" + "A" * 43 + ", t=1e3, sig=" + "A" * 86,
        "RainKey key=" + "A" * 43 + ", t=1, sig=" + "A" * 85,
        "RainKey key=" + "A" * 43 + ", t=1, sig=" + "A" * 86 + ", extra=1",
    ],
)
def test_the_header_has_exactly_one_shape(value):
    assert devicekeys.parse_header(value) is None


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://rainalerts.web.app", "https://rainalerts.web.app"),
        ("https://rainalerts.web.app/", "https://rainalerts.web.app"),
        ("https://RainAlerts.Web.App:443/", "https://rainalerts.web.app"),
        ("http://127.0.0.1:8097", "http://127.0.0.1:8097"),
    ],
)
def test_the_origin_is_spelled_the_way_a_browser_spells_it(url, origin):
    assert devicekeys.site_origin(url) == origin


# --- registering a key (§4.1) ----------------------------------------------------------------


def test_confirming_registers_the_key_and_opens_no_session(client, notifier, db):
    browser = enrolled(client, notifier)
    with db() as session:
        row = session.execute(select(DeviceKey)).scalars().one()
        assert row.id == browser.key_id and row.public_key == browser.spki


def test_push_subscribers_get_no_api_token_any_more(client, notifier, db):
    """The longest-lived credential a push subscriber had (§4.9)."""
    enrolled(client, notifier)
    with db() as session:
        purposes = session.execute(select(AuthToken.purpose)).scalars().all()
        assert TokenPurpose.API not in purposes


def test_a_page_from_before_the_release_confirms_as_before(client, notifier, db):
    """No `client` field: no proof asked, no key, the cookie session - a tab left open across the
    deploy keeps working."""
    response = confirm(client, subscribe_push(client, notifier), client_version="")
    assert response.status_code == 200
    assert MANAGE_COOKIE in response.cookies
    with db() as session:
        assert session.execute(select(DeviceKey)).first() is None


def test_another_browsers_confirmation_link_is_refused_and_not_spent(client, notifier, db):
    """Review 1, High: an attacker's own link, opened by a victim, must not bind the victim's
    browser to the attacker's subscription. Refused before anything is spent (review 3)."""
    token = subscribe_push(client, notifier)
    stranger = confirm(
        client,
        token,
        Browser(),
        endpoint="https://fcm.googleapis.com/fcm/send/other",
        p256dh="x" * 87,
    )
    assert stranger.status_code == 403
    assert MANAGE_COOKIE not in stranger.cookies
    with db() as session:
        assert session.execute(select(Subscriber)).scalars().one().confirmed_at is None
        assert session.execute(select(DeviceKey)).first() is None
    assert confirm(client, token, Browser()).status_code == 200, "the owner's link still works"


def test_the_p256dh_alone_proves_the_subscription(client, notifier):
    """A browser that re-encodes its endpoint string keeps its key (review 2)."""
    token = subscribe_push(client, notifier)
    response = confirm(client, token, Browser(), endpoint="")
    assert response.status_code == 200 and "data-key-enrolled" in response.text


def test_a_current_page_with_no_subscription_at_all_is_refused(client, notifier):
    """A current script that could not read its subscription sends nothing to compare."""
    token = subscribe_push(client, notifier)
    assert confirm(client, token, Browser(), endpoint="", p256dh="").status_code == 403


def test_a_key_that_does_not_parse_costs_the_shortcut_not_the_link(client, notifier, db):
    token = subscribe_push(client, notifier)
    response = client.post(
        "/confirm",
        data={"token": token, "client": "dk1", "endpoint": ENDPOINT, "device_key": "QUJD"},
    )
    assert response.status_code == 200
    assert MANAGE_COOKIE in response.cookies
    with db() as session:
        assert session.execute(select(DeviceKey)).first() is None


def test_redemptions_refuse_a_cross_site_post(client, notifier):
    """Login CSRF: another site making a visitor's browser redeem the other site's owner's token."""
    token = subscribe_push(client, notifier)
    foreign = {"Origin": "https://evil.example"}
    assert client.post("/confirm", data={"token": token}, headers=foreign).status_code == 403
    assert (
        client.post("/api/v1/manage/session", data={"token": token}, headers=foreign).status_code
        == 403
    )
    fetch_metadata = {"Sec-Fetch-Site": "cross-site", "Origin": "null"}
    assert client.post("/confirm", data={"token": token}, headers=fetch_metadata).status_code == 403
    # Our own pages send `Origin: null` (Referrer-Policy: no-referrer) - with `same-origin`.
    same = {"Sec-Fetch-Site": "same-origin", "Origin": "null"}
    assert client.post("/confirm", data={"token": token}, headers=same).status_code == 200


def test_a_settings_link_registers_a_key_too(client, notifier, db):
    enrolled(client, notifier)
    client.post("/api/v1/manage/link", json={"channel": "webpush", "address": ENDPOINT})
    token = link_token(notifier)
    fresh = Browser()
    response = client.post(
        "/api/v1/manage/session",
        data={
            "token": token,
            "client": "dk1",
            "endpoint": ENDPOINT,
            "p256dh": P256DH,
            "device_key": fresh.public,
        },
    )
    assert response.json() == {"enrolled": fresh.key_id}
    assert MANAGE_COOKIE not in response.cookies
    assert fresh.request(client, "GET", "/api/v1/subscriptions/me").status_code == 200


def test_another_browsers_settings_link_is_refused_and_not_spent(client, notifier):
    enrolled(client, notifier)
    client.post("/api/v1/manage/link", json={"channel": "webpush", "address": ENDPOINT})
    token = link_token(notifier)
    wrong = {"token": token, "client": "dk1", "endpoint": "", "p256dh": "x" * 87}
    response = client.post("/api/v1/manage/session", data=wrong)
    assert response.status_code == 403
    assert response.json()["detail"] == {"error": "push_mismatch"}
    right = dict(wrong, p256dh=P256DH)
    assert client.post("/api/v1/manage/session", data=right).status_code == 200


def test_subscribing_cannot_register_a_key(client, notifier, db):
    """`POST /subscriptions` is unauthenticated (§4.1, "why not at subscribe time")."""
    response = client.post(
        "/api/v1/subscriptions",
        json={
            "channel": "webpush",
            "lat": MUNICH[0],
            "lon": MUNICH[1],
            "endpoint": ENDPOINT,
            "p256dh": P256DH,
            "auth": "a" * 22,
            "device_key": Browser().public,
        },
    )
    assert response.status_code == 422  # unknown field, refused outright
    with db() as session:
        assert session.execute(select(DeviceKey)).first() is None


def test_email_confirmations_register_no_key(client, notifier, db):
    client.post(
        "/api/v1/subscriptions",
        json={"email": "a@example.com", "lat": MUNICH[0], "lon": MUNICH[1]},
    )
    token = notifier.sent[-1].text.split("/confirm#")[1].split("=", 1)[1].split()[0]
    response = client.post(
        "/confirm",
        data={"token": token, "client": "dk1", "device_key": Browser().public},
    )
    assert response.status_code == 200 and MANAGE_COOKIE in response.cookies
    with db() as session:
        assert session.execute(select(DeviceKey)).first() is None
        assert TokenPurpose.API in session.execute(select(AuthToken.purpose)).scalars().all()


# --- signed requests (§4.2) ------------------------------------------------------------------


def test_a_signed_request_reads_and_writes_without_any_session(client, notifier, db):
    browser = enrolled(client, notifier)
    me = browser.request(client, "GET", "/api/v1/subscriptions/me")
    assert me.status_code == 200
    assert me.headers["cache-control"] == "private, no-store"
    body = b'{"radius_m":500}'
    assert browser.request(client, "PATCH", "/api/v1/subscriptions/me", body).status_code == 204
    assert browser.request(client, "GET", "/api/v1/subscriptions/me").json()["radius_m"] == 500


def test_a_signature_over_another_body_is_refused(client, notifier):
    browser = enrolled(client, notifier)
    header = browser.header("PATCH", "/api/v1/subscriptions/me", b'{"radius_m":500}')
    response = client.patch(
        "/api/v1/subscriptions/me",
        content=b'{"radius_m":20000}',
        headers={"Authorization": header, "Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "not authorised", "no reason that would delete the key"


def test_a_failed_signature_never_falls_back_to_the_cookie(client, notifier):
    """A RainKey request is judged by its signature alone (review 2)."""
    token = subscribe_push(client, notifier)
    confirm(client, token, client_version="")  # legacy path: a cookie session in this client
    assert client.get("/api/v1/subscriptions/me").status_code == 200
    stranger = Browser()
    assert stranger.request(client, "GET", "/api/v1/subscriptions/me").status_code == 401


def test_a_cookie_is_not_needed_and_no_csrf_value_either(client, notifier):
    browser = enrolled(client, notifier)
    assert not client.cookies
    response = browser.request(client, "PATCH", "/api/v1/subscriptions/me", b'{"radius_m":750}')
    assert response.status_code == 204


def test_an_unknown_key_says_so_and_nothing_else_does(client, notifier):
    enrolled(client, notifier)
    response = Browser().request(client, "GET", "/api/v1/subscriptions/me")
    assert response.status_code == 401
    assert response.json()["detail"] == {"error": "unknown_key"}


def test_a_signature_outside_the_window_says_clock(client, notifier):
    browser = enrolled(client, notifier)
    old = int(time.time()) - devicekeys.CLOCK_WINDOW_SECONDS - 5
    response = browser.request(client, "GET", "/api/v1/subscriptions/me", t=old)
    assert response.json()["detail"] == {"error": "clock"}
    near = int(time.time()) - devicekeys.CLOCK_WINDOW_SECONDS + 5
    assert browser.request(client, "GET", "/api/v1/subscriptions/me", t=near).status_code == 200


def test_a_query_string_is_refused_rather_than_canonicalised(client, notifier):
    browser = enrolled(client, notifier)
    header = browser.header("GET", "/api/v1/subscriptions/me")
    response = client.get("/api/v1/subscriptions/me?x=1", headers={"Authorization": header})
    assert response.status_code == 401


def test_failed_signatures_are_limited_per_ip(db, settings, notifier):
    capped = settings.model_copy(update={"device_key_failure_limit_per_hour": 2})
    client = make_client(db, capped, notifier)
    browser = enrolled(client, notifier)
    bad = browser.header("GET", "/api/v1/subscriptions/me", t=int(time.time()))[:-4] + "AAAA"
    codes = [
        client.get("/api/v1/subscriptions/me", headers={"Authorization": bad}).status_code
        for _ in range(3)
    ]
    assert codes == [401, 401, 429]
    assert browser.request(client, "GET", "/api/v1/subscriptions/me").status_code == 200


def test_using_the_key_counts_as_being_seen(client, notifier, db):
    browser = enrolled(client, notifier)
    with db() as session:
        session.execute(select(Subscriber)).scalars().one().last_seen_at = None
        session.commit()
    browser.request(client, "GET", "/api/v1/subscriptions/me")
    with db() as session:
        assert session.execute(select(Subscriber)).scalars().one().last_seen_at is not None
        assert session.execute(select(DeviceKey)).scalars().one().last_used_at is not None


def test_unsubscribing_deletes_the_key(client, notifier, db):
    browser = enrolled(client, notifier)
    assert browser.request(client, "DELETE", "/api/v1/subscriptions/me").status_code == 204
    with db() as session:
        assert session.execute(select(DeviceKey)).first() is None
    response = browser.request(client, "GET", "/api/v1/subscriptions/me")
    assert response.json()["detail"] == {"error": "unknown_key"}


# --- rotation (§4.5) -------------------------------------------------------------------------


def test_a_key_rotates_itself_and_the_old_one_stops_working(client, notifier):
    browser = enrolled(client, notifier)
    fresh = Browser()
    body = json.dumps({"device_key": fresh.public}).encode()
    response = browser.request(client, "POST", "/api/v1/device-key/rotate", body)
    assert response.json() == {"rotated": fresh.key_id}
    assert fresh.request(client, "GET", "/api/v1/subscriptions/me").status_code == 200
    stale = browser.request(client, "GET", "/api/v1/subscriptions/me")
    assert stale.json()["detail"] == {"error": "unknown_key"}


def test_only_a_device_key_can_rotate(client, notifier):
    """A cookie session minting a key would turn two hours into standing access."""
    token = subscribe_push(client, notifier)
    confirm(client, token, client_version="")  # a cookie session
    body = {"device_key": Browser().public}
    assert client.post("/api/v1/device-key/rotate", json=body).status_code in (403, 401)


# --- the kill switch (§4.8) ------------------------------------------------------------------


def test_switched_off_nothing_is_registered_or_accepted(db, settings, notifier):
    on = make_client(db, settings, notifier)
    browser = enrolled(on, notifier)
    off = make_client(db, settings.model_copy(update={"device_key_login_enabled": False}), notifier)
    response = browser.request(off, "GET", "/api/v1/subscriptions/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "not authorised", "a plain 401 cannot wipe anyone's key"

    off.post("/api/v1/manage/link", json={"channel": "webpush", "address": ENDPOINT})
    redeemed = off.post(
        "/api/v1/manage/session",
        data={
            "token": link_token(notifier),
            "client": "dk1",
            "endpoint": ENDPOINT,
            "p256dh": P256DH,
            "device_key": Browser().public,
        },
    )
    assert "csrf" in redeemed.json(), "falls back to the cookie session"


# --- what went with the buttons and the sign-out ---------------------------------------------


def test_the_sign_out_route_is_gone(client):
    assert client.post("/api/v1/manage/logout").status_code in (404, 405)


def test_tapping_a_warning_counts_as_being_seen(client, notifier, db):
    from rainalert.tokens import locate_token

    enrolled(client, notifier)
    with db() as session:
        subscriber = session.execute(select(Subscriber)).scalars().one()
        subscriber.last_seen_at = None
        session.commit()
        token = locate_token(subscriber.id, "test-secret", 60)
    assert client.post("/api/v1/locate", json={"token": token}).json()["located"] is True
    with db() as session:
        assert session.execute(select(Subscriber)).scalars().one().last_seen_at is not None


# --- what the reader is told -----------------------------------------------------------------


def test_the_privacy_page_names_the_stored_key_and_no_page_names_a_feature(client):
    """§ 25 TDDDG wants the stored key named; the product wants no feature named after it - to
    the reader they are subscribed or not (PLAN_DEVICE_KEY.md §11)."""
    from pathlib import Path

    assert "öffentliche Teil eines Schlüssels" in client.get("/privacy").text
    templates = Path(__file__).resolve().parents[1] / "rainalert" / "api" / "templates"
    for template in templates.glob("*.html"):
        assert "Schnellzugang" not in template.read_text(encoding="utf-8"), template.name


def test_the_kill_switch_is_configuration():
    from pathlib import Path

    infra = Path(__file__).resolve().parents[1] / "infra"
    assert "DEVICE_KEY_LOGIN_ENABLED = tostring(var.device_key_login_enabled)" in (
        infra / "run.tf"
    ).read_text(encoding="utf-8")
    assert 'variable "device_key_login_enabled"' in (infra / "variables.tf").read_text(
        encoding="utf-8"
    )
