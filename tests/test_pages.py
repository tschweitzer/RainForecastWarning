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
    # The overlay is created in one place now, so that is where the single opacity has to be.
    module = client.get("/static/radar.js").text
    assert "opacity: opts.layerOpacity" in module
    assert "opacity: 0.75" not in module
    # And every page must hand it the real value rather than typing one of its own.
    for path in ("/", "/map", "/manage"):
        page = client.get(path).text
        assert "layerOpacity" in page
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
    # The pin moved into the shared module when the picker did; both pages that show one get
    # it from there, so there is one place for this to be true.
    module = client.get("/static/radar.js").text
    assert "L.divIcon" in module
    assert "<svg viewBox=" in module
    # The failure mode, spelled out: no raster icon from anywhere.
    assert "marker-icon" not in module
    page = module
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
        manage_link_message(local, "you@example.com", "T", uuid.uuid4()),
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
        token = notifier.sent[-1].text.split("/confirm#")[1].split("=", 1)[1].split()[0]
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


def test_the_app_link_leads_on_a_phone(client):
    """Replaces a test that pinned one ordering for everyone. The app link still comes first
    where it works - it is the only thing that subscribes in one tap - but the fallback under
    it is the store now, not ntfy's web page, and the desktop branch has neither."""
    page = client.get("/").text
    steps = page.split("function phoneSteps(scan)")[1].split("function browserSteps()")[0]
    # Install first, because neither the link nor the code does anything without the app.
    assert steps.index("installRow(") < steps.index("data.app_url")
    assert "In der ntfy-App öffnen und abonnieren" in page
    # On the phone only that phone's store; on a desktop both, because the page cannot know
    # what is in the reader's pocket.
    assert "installRow(scan ? null : RainPlatform.name())" in steps


def test_the_qr_encodes_the_app_link_not_the_web_one(client, settings, db):
    """Reversed on purpose, and this test with it.

    It is scanned by the phone that wants the warnings, and ntfy's own web page would subscribe
    *that phone* to web push - which its docs say needs iOS 16.4 and the page on the home
    screen, and which is the thing a native app was chosen to avoid. A custom scheme does
    nothing without the app, so the page says so in the step above the code.

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

    assert body["qr_svg"] == encoded(body["app_url"])
    assert body["qr_svg"] != encoded(body["subscribe_url"])


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
    message = settings_anchor_message(settings, "rainalert-abc", token, uuid.uuid4())

    (action,) = message.actions
    assert action.url == "https://rain.example.invalid/api/v1/manage/request"
    assert json.loads(action.body) == {"token": token}
    # A comma in the body would be read as the start of the next action parameter.
    assert "," not in action.body
    # The fallback for a client without buttons. In the fragment, never the query string, so it
    # cannot reach a server log or a Referer header (F-4/F-8).
    assert f"/manage#r={token}" in message.text
    assert "?token=" not in message.text


# --- the map is how a place is chosen ----------------------------------------------------


def test_both_picker_pages_offer_a_map_and_no_coordinate_fields(client):
    """Decimal degrees are not something anyone knows about where they live. The fields survive
    only as the fallback for a failed Leaflet load, which is why they are marked hidden."""
    for path in ("/", "/manage"):
        body = client.get(path).text
        assert 'id="map"' in body
        assert 'id="coord-fallback" hidden' in body
        # Not `required`: a required field that is hidden refuses the submit with a browser
        # message pointing at something invisible, which reads as the form being broken.
        fallback = body.split('id="coord-fallback"')[1].split("</div>")[0]
        assert "required" not in fallback


def test_hidden_survives_the_row_layout(client):
    """`hidden` is only `display:none` in the UA stylesheet, so `.row { display:flex }` beat it
    and the coordinate fallback was on screen while marked hidden. Found in Chromium."""
    assert "[hidden] { display: none !important; }" in client.get("/").text


def test_the_picker_map_does_not_depend_on_the_radar(client):
    """`has_map` says whether there is imagery to lay over the map. Picking a place needs the
    basemap, not the radar - gating the whole map on it left the signup page with no way to
    choose a location at all on a fresh install."""
    body = client.get("/").text
    assert "L.map(" in body and "RainRadar.picker(" in body

    # The precise invariant: whatever decides to hide the map must not consult the radar flag.
    # Asserting only that both strings exist passes with the flag moved back into that branch,
    # which is exactly the bug - so read the branch's own condition.
    head = body[: body.index("document.getElementById('map').hidden = true;")]
    condition = head[head.rindex("if (") :]
    assert "hasOverlay" not in condition, (
        "the map is hidden when no radar is configured; the basemap is what you aim with"
    )


def test_lead_and_radius_are_sliders_with_a_readable_value(client):
    """Both have a step and a range the number field never expressed, and both are judgements
    rather than figures anyone knows - a slider shows the whole scale you are choosing on."""
    body = client.get("/manage").text
    for field in ("lead", "radius"):
        row = body.split(f'id="{field}"')[0].rsplit("<input", 1)[-1] + f'id="{field}"'
        assert 'type="range"' in row, f"{field} is not a slider"
        assert f'id="{field}-value"' in body, f"{field} has no readout"
    # Metres below a kilometre, kilometres above: "7500 m" is a number you have to divide.
    assert "toLocaleString('de-DE'" in body


def test_the_hidden_coordinate_fields_carry_no_validation_constraints(client):
    """A constraint on a hidden field is a form that refuses to submit and cannot say why.

    The browser will not submit an out-of-range `min`/`max` input, and reports it by focusing
    the field - which is invisible, so nothing is shown and nothing is sent. Measured: a pin
    dropped outside Germany produced no request at all and an empty result area. `required`
    was already gone for this reason; the range attributes were the same trap.
    """
    for path in ("/", "/manage"):
        body = client.get(path).text
        # Bounded by the locate button that follows it in both templates, so the slice cannot
        # run past the block and pick up attributes belonging to other fields.
        fallback = body.split('id="coord-fallback"')[1].split("<button")[0]
        for attribute in ("required", 'min="', 'max="'):
            assert attribute not in fallback, f"{path}: {attribute} on a hidden field"


def test_a_place_outside_germany_is_refused_next_to_the_map(client):
    """The map lets you drop a pin anywhere; the radar covers Germany. "Prüfe die Eingaben"
    under the button does not tell anyone the problem is *where* they pointed."""
    body = client.get("/").text
    assert "außerhalb Deutschlands" in body
    # The same bounds the server enforces, so the page cannot drift from it.
    from rainalert.subscriptions import LAT_RANGE, LON_RANGE

    assert f"lat < {LAT_RANGE[0]:.0f} || lat > {LAT_RANGE[1]:.0f}" in body
    assert f"lon < {LON_RANGE[0]:.0f} || lon > {LON_RANGE[1]:.0f}" in body


# --- the signup result is different on each platform ---------------------------------------


def test_the_platform_is_decided_in_the_browser_and_never_sent(client):
    """Read from `navigator.userAgent`, not the request header, so the server never learns the
    platform: nothing to store, nothing to log, nothing to leak."""
    body = client.get("/").text
    assert "navigator.userAgent" in body
    # An iPad on iPadOS 13+ reports as a Macintosh; without the touch check a Mac user would be
    # offered a phone app in the App Store.
    assert "navigator.maxTouchPoints" in body


def test_the_store_links_are_the_ones_ntfy_publishes(client):
    """A custom scheme does nothing without the app, and the honest answer is the store - not
    ntfy's web page, which is web push on a phone, the thing a native app was chosen to avoid."""
    body = client.get("/").text
    assert "play.google.com/store/apps/details?id=io.heckel.ntfy" in body
    assert "f-droid.org/en/packages/io.heckel.ntfy/" in body
    assert "apps.apple.com/app/ntfy/id1625396347" in body


def test_the_desktop_branch_offers_the_qr_and_not_the_app_scheme(client):
    """A desktop browser has nothing registered for `ntfy://`, so offering it first - which is
    what every platform used to get - put a dead link above the only thing that works."""
    body = client.get("/").text
    desktop = body.split("if (here === 'desktop')")[1].split("} else {")[0]
    # Two routes, both closed, each naming the device it is about - the page cannot know where
    # the reader wants to be warned, so it asks instead of guessing and putting one first.
    assert desktop.count("'route'") == 2
    assert "phoneSteps(true)" in desktop  # true: hand off by QR
    assert "browserSteps()" in desktop

    # The branch after the desktop one, not the first `} else {` in the file - there are
    # earlier ones, and splitting on them silently slices the wrong code.
    mobile = body.split("if (here === 'desktop')")[1].split("} else {")[1]
    assert "phoneSteps(false)" in mobile  # false: tap the link, no QR to scan
    assert "browserSteps()" not in mobile, "web push on the phone is the weaker version"

    # And the switch means what it says: the QR is only built for the scanning case.
    steps = body.split("function phoneSteps(scan)")[1].split("function browserSteps()")[0]
    assert "if (scan) {" in steps
    assert steps.index("qrCode()") < steps.index("data.app_url")


def test_a_finished_signup_stops_being_a_form(client):
    """The button stayed live directly above the result, and pressing it again mints a second
    subscription with a second topic, silently replacing the one on screen. On a desktop that
    was the likely next move: the result began at y=868 of a 900-pixel viewport and the page did
    not scroll, so one press looked like nothing had happened."""
    body = client.get("/").text
    assert "function settled()" in body
    assert "document.getElementById('signup').hidden = true;" in body
    assert "scrollIntoView" in body
    # Both channels finish, not just push.
    assert body.count("settled();") >= 2
    # And there is a way back, because hiding the form removes the only one.
    assert "Von vorn anfangen" in body


def test_the_store_marks_need_no_third_party_request(client):
    """The official badges are images on Apple's and Google's servers. `img-src` does not allow
    them and should not: a request for a badge tells the store the visitor's IP on every signup.
    Drawn inline instead - simplified marks for recognising the app, not the badge artwork."""
    body = client.get("/").text
    assert "play.google.com/store/apps/details" in body  # the link, which is fine
    # but nothing is fetched from them to draw it
    for host in ("play.google.com/intl", "apple.com/app-store", "developer.apple.com"):
        assert host not in body
    assert "createElementNS('http://www.w3.org/2000/svg'" in body


def test_the_desktop_reader_is_asked_where_not_told(client):
    """Where the warnings should land is the reader's choice, not a platform we can sniff: the
    browser they are signing up in and the phone in their pocket are different devices."""
    body = client.get("/").text
    assert "wo willst du gewarnt werden?" in body
    # Each route says in its own summary which device it is about, while still closed.
    assert "Auf dem Handy" in body and "In diesem Browser" in body
    # The honest cost of the browser route, said where it is chosen rather than discovered.
    assert "Schläft der Rechner" in body


# --- the way out is on every message that follows the confirmation --------------------------


def _every_message(settings):
    """One place that knows all of them, so a message added later shows up here unclassified
    rather than quietly shipping without a way out."""
    import uuid as _uuid
    from types import SimpleNamespace

    from rainalert.api.mail import (
        alert_message,
        confirmation_message,
        deletion_receipt,
        manage_link_message,
        settings_anchor_message,
    )
    from rainalert.db.models import Channel

    who = _uuid.uuid4()
    subscriber = SimpleNamespace(id=who, address="rainalert-abc", channel=Channel.NTFY)
    subscription = SimpleNamespace(timezone="Europe/Berlin")
    payload = {
        "predicted_start_at": "2026-09-23T14:30:00+00:00",
        "cycle_time": "2026-09-23T14:00:00+00:00",
        "lead_minutes": 30,
        "peak_mm_5min": 0.4,
    }
    return {
        "confirmation": (confirmation_message(settings, "rainalert-abc", "T"), False),
        "anchor": (settings_anchor_message(settings, "rainalert-abc", "T", who), True),
        "manage link": (manage_link_message(settings, "rainalert-abc", "T", who), True),
        "alert": (alert_message(None, settings, subscriber, subscription, payload), True),
        "deletion receipt": (deletion_receipt(settings, "rainalert-abc", channel="ntfy"), False),
    }


def test_every_message_after_the_confirmation_offers_a_way_out(client, settings):
    """Somebody who wants to stop reaches for whichever message is in front of them. A settings
    link that offers no exit says "you can change this" while hiding the change they came for.
    """
    for name, (message, expected) in _every_message(settings).items():
        has_it = "Abmelden: " in message.text
        assert has_it is expected, f"{name}: unsubscribe link present={has_it}, wanted={expected}"


def test_the_confirmation_and_the_receipt_are_the_two_exceptions(client, settings):
    """Neither is a message you can act on: before confirming there is nothing to leave, and an
    unconfirmed signup deletes itself; after the receipt the subscription is already gone."""
    messages = _every_message(settings)
    assert messages["confirmation"][1] is False
    assert messages["deletion receipt"][1] is False
    # And the receipt says what *is* still to do on push - stop the app listening.
    assert "abbestellen" in messages["deletion receipt"][0].text


def test_the_unsubscribe_link_is_built_in_one_place(client, settings):
    """Three messages carry it. Built three times, they drift - and the shape matters: the token
    rides in the fragment so it cannot reach a request log (D-26)."""
    for name, (message, expected) in _every_message(settings).items():
        if not expected:
            continue
        assert "/unsubscribe#t=" in message.text, name
        assert "/unsubscribe?token=" not in message.text, name


# --- navigation -----------------------------------------------------------------------------

#: Every page a reader can land on. Listed here rather than discovered, so adding a route means
#: deciding whether it belongs in the navigation instead of finding out later that it does not
#: have any - which is how /confirm, /unsubscribe and /privacy became dead ends.
EVERY_PAGE = ("/", "/map", "/manage", "/confirm", "/unsubscribe", "/privacy")


def test_every_page_carries_the_same_navigation(client, db):
    for path in EVERY_PAGE:
        body = client.get(path).text
        nav = body.split('<nav class="site"')[1].split("</nav>")[0]
        for label in ("Start", "Regenradar", "Einstellungen"):
            assert label in nav, f"{path} is missing {label}"


def test_the_navigation_sits_between_the_content_and_the_footer(client, db):
    """Always in the same place, so it is found by habit rather than by looking."""
    for path in EVERY_PAGE:
        body = client.get(path).text
        assert body.index('<nav class="site"') < body.index("<footer>"), path


def test_the_page_you_are_on_is_marked_and_is_not_a_link(client, db):
    """The set keeps its shape as you move around - the current entry is marked, not dropped."""
    import re

    for path, label in (("/", "Start"), ("/map", "Regenradar"), ("/manage", "Einstellungen")):
        nav = client.get(path).text.split('<nav class="site"')[1].split("</nav>")[0]
        # Whitespace-insensitive: the assertion is about which element wraps the label, not
        # about how Jinja happened to indent it.
        current = re.search(r'<strong aria-current="page">\s*([^<\s]+)', nav)
        assert current and current.group(1) == label, (
            f"{path}: marked {current and current.group(1)}"
        )
        assert f'href="{path}"' not in nav, f"{path} links to itself"


def test_the_radar_is_listed_even_with_no_overlay_store(client, db):
    """`has_map` says whether there is imagery to lay over the map, not whether the page exists:
    it renders its graticule and its "no radar data yet" banner perfectly well without one.
    Gating the link on it left the map missing from the navigation while standing on it."""
    # This client has no overlay store configured, which is the case under test.
    assert "Noch keine Radardaten" in client.get("/static/radar.js").text
    for path in EVERY_PAGE:
        nav = client.get(path).text.split('<nav class="site"')[1].split("</nav>")[0]
        assert "Regenradar" in nav, path


def test_the_old_one_off_wayfinding_links_are_gone(client, db):
    """The front page used to be reached as "Zur Anmeldung" from the map, "Zur Startseite" from
    the settings and nothing at all from three other pages. One name, one place."""
    for path in EVERY_PAGE:
        body = client.get(path).text
        for stale in ("Zur Anmeldung", "Zur Startseite", "Regenradar ansehen"):
            assert stale not in body, f"{path} still has its own {stale!r}"


def test_the_warning_link_is_explained_where_people_look_for_privacy(client, db):
    """A stored location becoming visible again from outside the settings page is a user-facing
    fact, not only a design note: it belongs on the page that says what happens to the data."""
    body = client.get("/privacy").text
    assert "Der Link in einer Warnung" in body
    # The two things that make it acceptable, both stated rather than implied. Matched on the
    # ASCII part: Jinja escapes the umlaut, so the literal German would never be found.
    assert "deinen Standort nicht" in body
    assert "60 Minuten" in body


def test_the_api_table_lists_the_endpoints_that_exist(client, db):
    """The table is the map of the service. Two endpoints had been added without it - which is
    how a reader ends up believing the surface is smaller than it is."""
    import pathlib
    import re

    design = pathlib.Path("docs/DESIGN.md").read_text()
    table = design[design.index("| Method | Path | Auth | Purpose |") :]
    table = table[: table.index("\n\n")]
    routes = {
        r.path
        for r in client.app.routes
        if getattr(r, "path", "").startswith("/api/v1/") and "{" not in getattr(r, "path", "")
    }
    for path in routes:
        short = path.replace("/api/v1", "")
        assert re.search(rf"`{re.escape(short)}[`#?/]", table), f"{path} is not in the API table"
