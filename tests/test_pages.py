"""The pages' own assets and wiring.

Browser behaviour cannot be asserted here - geolocation needs a browser, and that was checked
against Chromium separately. What these tests hold in place is the wiring that made the old
button silently do nothing: the helper being served at all, every page actually loading it, and
each page having somewhere to put a message when the attempt fails.
"""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from rainalert.api.app import create_app
from rainalert.api.mail import settings_anchor_message
from rainalert.config import Settings
from rainalert.notify import ConsoleNotifier, MessageAction, OutboundMessage
from rainalert.notify.ntfy import MAX_ACTIONS, NtfyNotifier, _actions_header
from rainalert.tokens import (
    manage_request_token,
    session_token,
    verify_manage_request_token,
    verify_session_token,
)


@pytest.fixture()
def settings():
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.invalid",
        secret_key="test-secret",
        notifier="console",
        _env_file=None,
    )


@pytest.fixture()
def notifier():
    return ConsoleNotifier()


@pytest.fixture()
def client(db, settings):
    app = create_app(settings, session_factory=db, notifier=ConsoleNotifier())
    return TestClient(app, base_url=settings.public_base_url)


def test_the_geolocation_helper_is_served(client):
    response = client.get("/static/geolocate.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/", "/map", "/manage"])
def test_every_page_with_a_locate_button_loads_the_helper(client, path):
    assert "/static/geolocate.js" in client.get(path).text


@pytest.mark.parametrize("path", ["/", "/manage"])
def test_the_form_pages_have_somewhere_to_show_a_failure(client, path):
    # Without this element the handler has nowhere to report, which is how the button came to
    # fail silently in the first place.
    assert 'id="locate-status"' in client.get(path).text


def test_the_map_page_has_an_on_map_control(client):
    page = client.get("/map").text
    assert "locate-control" in page
    assert "Zu meinem Standort" in page


@pytest.mark.parametrize(
    "fragment",
    [
        "isSecureContext",  # the http-on-a-VM case, which no page code can fix
        "timeout",  # or a browser that never answers leaves the button busy forever
        "error.code === 1",  # denied
        "error.code === 2",  # position unavailable
        "error.code === 3",  # timed out
    ],
)
def test_the_helper_handles_every_way_geolocation_declines(client, fragment):
    """Each of these is a path that used to end in silence."""
    assert fragment in client.get("/static/geolocate.js").text


def test_the_csp_allows_the_helper_but_not_arbitrary_script(client):
    policy = client.get("/").headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "'unsafe-inline'" not in policy.split("style-src")[0]


# --- readiness ---------------------------------------------------------------------------------


def test_readyz_is_ready_on_a_current_schema(client):
    assert client.get("/readyz").status_code == 200


def test_readyz_refuses_a_schema_that_is_behind(client, db):
    """The failure this exists for: new code deployed over an un-migrated database.

    It connects fine and then 500s on the first request touching what the migration added, a
    long way from the cause. Readiness is where that belongs.
    """
    from sqlalchemy import text

    with db() as session:
        session.execute(text("UPDATE alembic_version SET version_num = 'deadbeef1234'"))
        session.commit()

    response = client.get("/readyz")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "deadbeef1234" in detail  # what it is
    assert "make migrate" in detail  # what to do about it


def test_healthz_stays_up_when_the_schema_is_behind(client, db):
    """Liveness is not readiness: the process is fine, it just must not take traffic."""
    from sqlalchemy import text

    with db() as session:
        session.execute(text("UPDATE alembic_version SET version_num = 'deadbeef1234'"))
        session.commit()
    assert client.get("/healthz").status_code == 200


def test_a_database_with_no_alembic_version_is_reported(client, db):
    from sqlalchemy import text

    with db() as session:
        session.execute(text("DROP TABLE alembic_version"))
        session.commit()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "no schema yet" in response.json()["detail"]


# --- the intensity scale ------------------------------------------------------------------------


def test_the_dropdown_and_the_map_legend_come_from_one_source(client):
    """The point of the change: a colour means the same rain on both pages.

    Asserted structurally rather than by comparing two hard-coded lists, because two lists is
    exactly the failure being designed out.
    """
    from rainalert.radar.overlay import INTENSITY_BANDS, legend

    page = client.get("/manage").text
    for threshold, rgba, label in INTENSITY_BANDS:
        assert f'value="{threshold}"' in page, f"{label} missing from the dropdown"
        assert f"rgba({rgba[0]},{rgba[1]},{rgba[2]}," in page
        assert label in page
    # Kept short: the option is a name and a threshold. The hourly equivalent is an
    # extrapolation that needs a sentence to be honest, and there is no room for one in a
    # dropdown - it stays on the map legend, where it is a tooltip.
    assert "mm/h" not in page
    # The same list the map draws its legend from.
    assert [b["from_mm_5min"] for b in legend()] == [t for t, _, _ in INTENSITY_BANDS]


def test_the_renderer_still_sees_only_thresholds_and_colours():
    """Adding names must not change what the overlay is drawn from."""
    from rainalert.radar.overlay import COLOR_STOPS, INTENSITY_BANDS

    assert COLOR_STOPS == tuple((t, c) for t, c, _ in INTENSITY_BANDS)


def test_the_bands_are_ordered_and_within_what_can_be_stored(settings):
    from rainalert.radar.overlay import INTENSITY_BANDS

    thresholds = [t for t, _, _ in INTENSITY_BANDS]
    assert thresholds == sorted(thresholds)
    for threshold in thresholds:
        # Every option in the dropdown must be a value the API will actually accept, or the page
        # offers choices that fail on save.
        assert settings.min_threshold_mm_5min <= threshold <= settings.plausibility_max_mm_5min
        assert round(threshold, 2) == threshold  # numeric(5,2) keeps two decimals


def test_every_band_is_accepted_by_the_api(settings):
    """Belt and braces on the above: the values are offered, so they must save."""
    from rainalert import subscriptions as svc
    from rainalert.radar.overlay import INTENSITY_BANDS

    for threshold, _, label in INTENSITY_BANDS:
        assert svc.validate_rule(settings, threshold_mm_5min=threshold) == {
            "threshold_mm_5min": threshold
        }, label


def test_a_new_subscription_lands_on_a_named_band(db, settings):
    """The reason the default is 0.15 and not a rounder 0.10.

    A default that is not one of the bands shows up on the settings page as "eigener Wert",
    which is a confusing first impression for something nobody chose.
    """
    from rainalert import subscriptions as svc
    from rainalert.db.models import Subscription
    from rainalert.radar.overlay import INTENSITY_BANDS

    with db() as session:
        svc.subscribe(session, settings, lat=50.1, lon=8.6, address="fresh@example.com")
        stored = float(session.query(Subscription).one().threshold_mm_5min)

    assert stored in [threshold for threshold, _, _ in INTENSITY_BANDS]


def test_every_default_threshold_agrees(settings):
    """Four places carry it; they are all the same number or one of them is a bug."""
    from rainalert.alerting.rules import AlertRule
    from rainalert.cli import DEFAULT_THRESHOLD
    from rainalert.db.models import Subscription

    column = Subscription.__table__.c.threshold_mm_5min.default.arg
    assert float(column) == settings.default_threshold_mm_5min
    assert AlertRule().threshold_mm_5min == settings.default_threshold_mm_5min
    assert DEFAULT_THRESHOLD == settings.default_threshold_mm_5min


def test_there_is_only_one_opacity(client):
    """The palette's alpha used to be multiplied again by the Leaflet layer's.

    That put the lightest band at an effective 0.38 on the map and 0.31 on the settings page -
    invisible over a basemap - and made the palette impossible to reason about, because no
    number in it was the number you saw.
    """
    from rainalert.radar.overlay import LAYER_OPACITY

    assert LAYER_OPACITY == 1.0
    for path in ("/map", "/manage"):
        page = client.get(path).text
        assert "opacity: LAYER_OPACITY" in page or "opacity: LAYER_OPACITY}" in page
        # No second opacity typed into the template to multiply it back down.
        assert "opacity: 0.75" not in page
        assert "opacity: 0.6}" not in page


def test_the_lightest_band_is_actually_visible():
    """A floor, so the faintest rain cannot drift back to invisible unnoticed."""
    from rainalert.radar.overlay import INTENSITY_BANDS

    lightest = INTENSITY_BANDS[0][1][3] / 255
    assert lightest >= 0.5, f"the lightest band is at {lightest:.2f} over the basemap"


def test_the_bands_get_more_opaque_as_the_rain_gets_heavier():
    from rainalert.radar.overlay import INTENSITY_BANDS

    alphas = [rgba[3] for _, rgba, _ in INTENSITY_BANDS]
    assert alphas == sorted(alphas)
    assert max(alphas) <= 255


# --- the signup page says true things about both channels ---------------------------------------


def test_the_consent_text_has_a_wording_for_each_channel(client):
    """Push subscribers have no email address; telling them one is stored is simply false."""
    page = " ".join(client.get("/").text.split())  # the template wraps; the sentence does not
    assert "Ich bin einverstanden, dass mein Push-Thema und mein Standort gespeichert" in page
    assert "Ich bin einverstanden, dass meine E-Mail-Adresse und mein Standort gespeichert" in page


def test_the_signup_note_does_not_claim_nothing_is_stored(client):
    """It was not true: `subscribe` writes a pending row before anyone confirms.

    The privacy page always said unconfirmed signups are deleted after a while, so the front
    page was contradicting it - in a consent notice, which is the worst place for it.
    """
    page = client.get("/").text
    assert "Ohne Bestätigung wird nichts gespeichert" not in page
    assert "Stunden gelöscht" in page


def test_both_channel_wordings_are_in_the_page_source(client):
    """Rendered, not assembled by script - consent should be readable in the page itself."""
    page = client.get("/").text
    assert page.count('class="for-ntfy"') >= 2
    assert page.count('class="for-email"') >= 2


def test_the_stored_consent_record_names_the_channel_and_the_version(db, settings):
    """Two wordings share a version, so the channel is what disambiguates them."""
    from rainalert import subscriptions as svc
    from rainalert.db.models import Channel, Subscriber

    with db() as session:
        svc.subscribe(session, settings, lat=50.1, lon=8.6, channel=Channel.NTFY)
        row = session.query(Subscriber).one()
        assert row.channel is Channel.NTFY
        assert row.consent_text_version == settings.consent_text_version


def test_the_privacy_page_covers_both_channels(client):
    page = client.get("/privacy").text
    assert "ntfy-Thema" in page
    assert "E-Mail-Adresse" in page
    # The public server sees the message text and the topic name; that belongs on this page.
    assert "ntfy-Server" in page


def test_the_session_control_is_outside_the_settings_form(client):
    """Next to Save, anything button-shaped reads as Cancel."""
    page = client.get("/manage").text
    form = page.split('id="settings-form"')[1].split("</form>")[0]
    assert 'id="logout"' not in form
    assert "Sitzung auf diesem Gerät beenden" in page
    # It ends the session and nothing else, so it must not say "Abmelden", which in German is
    # also what you call cancelling a subscription.
    assert "Abmelden (nur dieses Gerät)" not in page


def test_the_subscribe_page_links_to_the_settings_page(client):
    """Someone already subscribed lands on / looking for their settings."""
    assert 'href="/manage"' in client.get("/").text


@pytest.mark.parametrize("path", ["/map", "/manage"])
def test_both_maps_zoom_to_street_level(client, path):
    assert "maxZoom: 18" in client.get(path).text


# --- the map marker ------------------------------------------------------------------------------


def test_the_marker_icon_is_inline_and_needs_no_network(client):
    """Leaflet's default marker is a PNG fetched from wherever the library came from.

    `img-src` does not allow unpkg - deliberately, it is a third party that would learn the
    visitor's IP on every map view - so the browser refused it and drew the broken-image
    placeholder with its alt text. Confirmed in Chromium: "Refused to load the image ...
    because it violates the following Content Security Policy directive".
    """
    page = client.get("/manage").text
    assert "L.divIcon" in page
    assert "<svg viewBox=" in page
    # The failure mode, spelled out: no raster icon from anywhere.
    assert "marker-icon" not in page
    # The anchor must be the bottom centre of whatever size the pin is, or the point of the
    # pin stops marking the coordinate it is there to mark.
    size = re.search(r"iconSize: \[(\d+), (\d+)\]", page)
    anchor = re.search(r"iconAnchor: \[(\d+), (\d+)\]", page)
    assert size and anchor
    width, height = int(size[1]), int(size[2])
    assert (int(anchor[1]), int(anchor[2])) == (width // 2, height)


def test_the_policy_was_not_widened_to_fix_the_marker(client):
    """The other way to make the icon appear would have been to allow unpkg in img-src.

    That trades a drawing problem for a privacy one - an image request is a page view reported
    to a CDN - so it must stay refused, and this says so out loud.
    """
    policy = client.get("/manage").headers["content-security-policy"]
    img_src = next(part for part in policy.split(";") if part.strip().startswith("img-src"))
    assert "unpkg" not in img_src
    assert "'self'" in img_src and "data:" in img_src


def test_no_page_relies_on_a_third_party_image(client):
    """Scripts and styles come from the CDN until Leaflet is vendored; images must not."""
    for path in ("/", "/map", "/manage"):
        page = client.get(path).text
        for marker in ('<img src="https://', "src: 'https://", "iconUrl"):
            assert marker not in page, f"{path} pulls an image from elsewhere"


# --- attribution (DESIGN.md 4.2) ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/map", "/manage", "/privacy"])
def test_every_page_credits_dwd_and_says_the_data_was_modified(client, path):
    """CC BY 4.0 wants the source, the licence, and any modification indicated.

    This service reprojects the RADOLAN grid to Web Mercator, coarsens it to ~2 km and turns it
    into colours, so the third part is not optional - and it was the part that was missing.
    """
    page = client.get(path).text
    assert "Deutscher Wetterdienst" in page
    assert "creativecommons.org/licenses/by/4.0" in page
    assert "eigene Verarbeitung" in page


def test_the_messages_carry_the_same_credit(settings):
    from rainalert.api.mail import confirmation_message
    from rainalert.attribution import ATTRIBUTION

    message = confirmation_message(settings, "friend@example.com", "token")
    assert ATTRIBUTION in message.text
    assert "eigene Verarbeitung" in message.text


def test_the_credit_is_defined_once(client):
    """Four copies of a string that must agree are four that will not."""
    from rainalert.attribution import ATTRIBUTION, ATTRIBUTION_HTML

    for text in (ATTRIBUTION, ATTRIBUTION_HTML):
        assert "Deutscher Wetterdienst" in text
        assert "eigene Verarbeitung" in text
    # The plain-text form goes into mail headers and JSON; keep it ASCII.
    ATTRIBUTION.encode("ascii")


def test_the_timeline_payload_carries_the_credit(client, db, settings):
    """The map draws from JSON, so the credit has to be in the JSON."""
    from rainalert.attribution import ATTRIBUTION
    from rainalert.timeline import build_timeline

    with db() as session:
        payload = build_timeline(session, settings, _NullStore(), 12)
    assert payload["attribution"] == ATTRIBUTION


class _NullStore:
    """Enough OverlayStore for build_timeline to produce a payload with no cycles stored."""

    def url_for_observed(self, nominal_time):
        return "/overlays/obs/x.png"

    def url_for_forecast(self, nominal_time, lead_minutes):
        return "/overlays/fc/x.png"


# --- the development base URL (Q-10) -------------------------------------------------------------


def test_every_message_link_follows_the_configured_base_url(db, settings):
    """An IP over http is all a VM needs, and needs no code - this is why.

    Nothing in the code knows a hostname; every link is built from PUBLIC_BASE_URL. The test
    exists so that stays true: a link hard-coded anywhere would fail here.
    """
    from rainalert.api.mail import confirmation_message, deletion_receipt, manage_link_message

    local = settings.model_copy(update={"public_base_url": "http://203.0.113.10:8000"})
    messages = [
        confirmation_message(local, "you@example.com", "T"),
        confirmation_message(local, "rainalert-abc", "T", channel="ntfy"),
        manage_link_message(local, "you@example.com", "T"),
        deletion_receipt(local, "you@example.com"),
    ]
    for message in messages:
        for word in message.text.split():
            if word.startswith("http") and "creativecommons" not in word:
                assert word.startswith("http://203.0.113.10:8000"), word
        if message.click_url:
            assert message.click_url.startswith("http://203.0.113.10:8000")


def test_the_session_cookie_secure_flag_follows_the_same_setting(db, notifier, settings):
    """Q-10's other half: moving to https is one setting, not a checklist.

    The flag cannot simply be on - a Secure cookie over http is discarded and the login looks
    broken - so it keys off the base URL, which means switching the scheme switches this too.
    """
    from rainalert.api.app import create_app

    cases = (("http://203.0.113.10:8000", False), ("https://rain.example", True))
    for index, (base, expect_secure) in enumerate(cases):
        # A fresh address per case: a confirmed subscriber is not sent another confirmation,
        # so reusing one leaves the second case reading the first case's message.
        address = f"q10-{index}@example.com"
        app = create_app(settings.model_copy(update={"public_base_url": base}), db, notifier)
        client = TestClient(app, base_url=base)
        client.post("/api/v1/subscriptions", json={"email": address, "lat": 50.1, "lon": 8.6})
        token = notifier.sent[-1].text.split("/confirm#t=")[1].split()[0]
        client.post("/confirm", data={"token": token})
        client.post("/api/v1/manage/link", json={"channel": "email", "address": address})
        link = notifier.sent[-1].text.split("/manage#t=")[1].split()[0]
        response = client.post("/api/v1/manage/session", data={"token": link})
        assert ("secure" in response.headers["set-cookie"].lower()) is expect_secure, base


# --- the ntfy deep link ---------------------------------------------------------------------------


def test_the_subscribe_response_offers_an_app_link_and_a_web_link(client, db):
    """One tap subscribes; the other always resolves. Neither covers everyone alone."""
    response = client.post(
        "/api/v1/subscriptions", json={"channel": "ntfy", "lat": 48.15, "lon": 11.55}
    )
    assert response.status_code == 202
    body = response.json()
    topic = body["topic"]
    assert body["app_url"].startswith(f"ntfy://ntfy.sh/{topic}")
    assert body["subscribe_url"] == f"https://ntfy.sh/{topic}"


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        # https is what the app assumes, so it needs no parameter.
        ("https://ntfy.sh", "ntfy://ntfy.sh/t?display=Regenwarnung"),
        ("https://push.example.org/", "ntfy://push.example.org/t?display=Regenwarnung"),
        # A self-hosted server on plain http has to say so, or the app tries https and fails.
        ("http://10.0.0.5:8080", "ntfy://10.0.0.5:8080/t?secure=false&display=Regenwarnung"),
    ],
)
def test_the_deep_link_follows_ntfys_documented_forms(server, expected):
    from rainalert.notify.ntfy import deep_link

    assert deep_link(server, "t") == expected


def test_the_deep_link_escapes_the_topic():
    """The topic is generated by us, but the escaping is the difference between a URL and a
    string that happens to look like one."""
    from rainalert.notify.ntfy import deep_link

    assert "a%2Fb" in deep_link("https://ntfy.sh", "a/b")


def test_the_deep_link_refuses_a_server_it_cannot_parse():
    from rainalert.notify.ntfy import deep_link

    with pytest.raises(ValueError):
        deep_link("", "topic")


def test_the_page_offers_the_app_link_before_the_web_one(client):
    """The web link is the fallback for a missing app, so it must read as one."""
    page = client.get("/").text
    assert page.index("data.app_url") < page.index("data.subscribe_url")
    assert "In der ntfy-App öffnen und abonnieren" in page
    assert "Passiert nichts? Dann ist die App nicht installiert" in page


def test_the_qr_still_encodes_the_web_link_not_the_app_one(client, settings, db):
    """A phone camera will not open a custom scheme, and the QR exists for the desktop case -
    sign up on a laptop, scan with a phone. The web page has a subscribe button on it.

    Checked by re-encoding both candidates and seeing which one matches, because an SVG that is
    merely well formed would pass whatever URL went into it.
    """
    import io

    import segno

    def encoded(text):
        buffer = io.BytesIO()
        segno.make(text, error="m").save(
            buffer, kind="svg", scale=4, xmldecl=False, omitsize=True, svgclass=None
        )
        return buffer.getvalue().decode("utf-8")

    body = client.post(
        "/api/v1/subscriptions", json={"channel": "ntfy", "lat": 50.11, "lon": 8.68}
    ).json()

    assert body["qr_svg"] == encoded(body["subscribe_url"])
    assert body["qr_svg"] != encoded(body["app_url"])


# --- the settings button on a notification -----------------------------------------------------


def _ntfy_settings(**kwargs):
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.invalid",
        mail_from="RainAlert <noreply@rain.example.invalid>",
        secret_key="test-secret",
        _env_file=None,
        **kwargs,
    )


def test_the_action_header_follows_ntfys_documented_short_form():
    header = _actions_header(
        (
            MessageAction(
                label="Einstellungen", url="https://rain.example.invalid/x", body='{"token":"a"}'
            ),
        )
    )
    assert header == (
        "http, Einstellungen, https://rain.example.invalid/x, method=POST, "
        'headers.Content-Type=application/json, body={"token":"a"}'
    )


@pytest.mark.parametrize("bad", ["a,b", "a;b", '"a', "'a", "a\nb"])
def test_an_action_refuses_a_value_that_would_break_the_header(bad):
    """The header's separators are the comma and the semicolon; a value carrying one would
    silently become a second action, or a malformed one."""
    with pytest.raises(ValueError):
        MessageAction(label=bad, url="https://rain.example.invalid/x")


def test_more_actions_than_ntfy_renders_is_refused_rather_than_silently_dropped():
    action = MessageAction(label="x", url="https://rain.example.invalid/x")
    with pytest.raises(ValueError):
        _actions_header((action,) * (MAX_ACTIONS + 1))


def test_the_notifier_sends_the_action_header_only_when_there_is_an_action():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200)

    notifier = NtfyNotifier(transport=httpx.MockTransport(handler))
    action = MessageAction(label="Einstellungen", url="https://rain.example.invalid/x", body="t=1")
    notifier.send(OutboundMessage(to="topic", subject="s", text="t"))
    notifier.send(OutboundMessage(to="topic", subject="s", text="t", actions=(action,)))

    assert "Actions" not in seen[0]
    assert seen[1]["Actions"].startswith("http, Einstellungen,")


def test_a_request_token_round_trips_and_is_not_interchangeable_with_a_session():
    """Purpose is inside the MAC, so the durable token cannot be spent as a session and the
    session cookie cannot be replayed at the request endpoint."""
    subscriber_id = uuid.uuid4()
    request = manage_request_token(subscriber_id, "secret", 365)
    assert verify_manage_request_token(request, "secret").subscriber_id == subscriber_id

    assert verify_session_token(request, "secret") is None
    session = session_token(subscriber_id, "secret", 30)
    assert verify_manage_request_token(session, "secret") is None
    assert verify_manage_request_token(request, "other-secret") is None


def test_a_request_token_stops_working_once_it_is_old():
    subscriber_id = uuid.uuid4()
    minted = datetime.now(UTC)
    token = manage_request_token(subscriber_id, "secret", 1, now=minted)
    assert verify_manage_request_token(token, "secret", minted + timedelta(hours=23)) is not None
    assert verify_manage_request_token(token, "secret", minted + timedelta(days=1)) is None


def test_the_anchor_message_carries_the_button_and_a_fallback_in_the_fragment():
    settings = _ntfy_settings()
    token = manage_request_token(uuid.uuid4(), settings.secret_key, 365)
    message = settings_anchor_message(settings, "rainalert-abc", token)

    (action,) = message.actions
    assert action.url == "https://rain.example.invalid/api/v1/manage/request"
    assert json.loads(action.body) == {"token": token}
    # A comma in the body would be read as the start of the next action parameter.
    assert "," not in action.body
    # The fallback for a client without buttons. In the fragment, never the query string, so it
    # cannot reach a server log or a Referer header (F-4/F-8).
    assert f"/manage#r={token}" in message.text
    assert "?token=" not in message.text
