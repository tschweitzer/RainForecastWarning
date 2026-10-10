"""The pages' own assets and wiring.

Browser behaviour cannot be asserted here - geolocation needs a browser, and that was checked
against Chromium separately. What these tests hold in place is the wiring that made the old
button silently do nothing: the helper being served at all, every page actually loading it, and
each page having somewhere to put a message when the attempt fails.
"""

import json
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rainalert.api.app import create_app
from rainalert.config import Settings
from rainalert.notify import ConsoleNotifier, OutboundMessage
from tests.helpers import page_source


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


@pytest.mark.parametrize("path", ["/", "/manage"])
def test_every_page_with_a_locate_button_loads_the_helper(client, path):
    assert "/static/geolocate.js" in client.get(path).text


@pytest.mark.parametrize("path", ["/", "/manage"])
def test_the_form_pages_have_somewhere_to_show_a_failure(client, path):
    # Without this element the handler has nowhere to report, which is how the button came to
    # fail silently in the first place.
    assert 'id="locate-status"' in client.get(path).text


def test_every_map_has_an_on_map_locate_control(client, db):
    """A map control belongs on the map, in the corner a map keeps its controls.

    The radar page had one and the other two had a button underneath instead; now all three use
    the same one from the shared module, which is where the markup lives - so this follows it
    there rather than looking for it in whichever page happened to hold a copy.
    """
    module = client.get("/static/radar.js").text
    assert "locate-control" in module
    assert "Zu meinem Standort" in module
    # Leaflet's own prescription for a custom control. It matters most on the picker maps, where
    # a click that reached the map would move the subscriber's pin to wherever the button is.
    assert "L.DomEvent.disableClickPropagation" in module

    # The vector map trial's engine (D-58) has its own, with the same guard in DOM terms.
    gl = client.get("/static/radar-gl.js").text
    assert "locate-control" in gl and "Zu meinem Standort" in gl
    assert "event.stopPropagation();" in gl

    # Both pages call whichever engine they chose (D-59).
    for path in ("/", "/manage"):
        assert "Engine.locateControl(" in page_source(client, path), path


def enclosing_ids(markup: str, element_id: str) -> list[str]:
    """The ids of the elements enclosing the one with this id, outermost first.

    Comparing offsets in the source would not answer the question: an element written just
    *after* a block sits at a later offset than the block's own tag, exactly like one written
    inside it. Only the tag stack tells them apart.
    """
    void = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }

    class Walker(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[str | None] = []
            self.found: list[str] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            found_id = dict(attrs).get("id")
            if found_id == element_id and self.found is None:
                self.found = [i for i in self.stack if i is not None]
            if tag not in void:
                self.stack.append(found_id)

        def handle_startendtag(self, tag, attrs) -> None:
            self.handle_starttag(tag, attrs)

        def handle_endtag(self, tag: str) -> None:
            if tag not in void and self.stack:
                self.stack.pop()

    walker = Walker()
    walker.feed(markup)
    assert walker.found is not None, f"no element with id={element_id!r}"
    return walker.found


@pytest.mark.parametrize("path", ["/", "/manage"])
def test_a_picker_page_keeps_a_plain_locate_button_only_without_a_map(client, db, path):
    """With a map there is a control on it. Without one - a blocked CDN - the coordinate fields
    are all that is left, and typing decimal degrees should not be the only way through."""
    body = client.get(path).text
    assert 'id="locate"' in body
    # Inside the block that stays hidden until Leaflet fails, not merely somewhere after it:
    # a button beside the map would show up permanently, which is what this replaced.
    assert "coord-fallback" in enclosing_ids(body, "locate"), path
    assert "coord-fallback" in enclosing_ids(body, "locate-status"), path


def test_every_page_a_notification_can_open_re_reads_its_fragment(client):
    """The trap that has now caught three pages, and the rule that closes it for all of them.

    Every token this service hands out rides in the URL *fragment* (D-26), because a fragment never
    reaches the server. Every page that receives one reads it at load and immediately erases it with
    `replaceState`, so a tab sitting on that page has a bare path in its address bar.

    `focusOrOpen` in sw.js then reuses that tab by navigating it - deliberately, because opening a
    window instead left one tab per notification. When the target path equals the open tab's path,
    `client.navigate()` changes only the fragment, and that is a *same-document* navigation: no
    script re-runs, the token is never read, and the reader taps the notification and watches
    nothing happen.

    It was fixed on `/` for warning links, and stayed broken on `/manage` until someone pressed
    "Link an diesen Browser senden", stayed on the page and tapped the notification - the tell being
    that navigating away first made it work. `/confirm` had it too, unreported and worse: a signup
    that is never confirmed is purged without explanation.

    So this enumerates the paths from the click_urls the messages actually build, rather than from a
    list someone has to remember to extend. A new notification target fails here until its page can
    be re-entered.
    """
    import ast
    import re
    from pathlib import Path

    mail = (Path(__file__).resolve().parents[1] / "rainalert" / "api" / "mail.py").read_text(
        encoding="utf-8"
    )
    # The paths a click_url can point at.
    #
    # Scoped to the functions that actually build one, which is what makes this precise. Matching
    # every `public_base_url` URL with a fragment in the file also catches `/unsubscribe#t=`, and
    # that one is email-only: it goes in a mail body, never in a `click_url`, so no notification can
    # ever open it. (Notifications carry no buttons, D-64.)
    #
    # Matched across string concatenation, because the warning link is built as `f"...}}/"` on one
    # line and `f"#l={...}"` on the next; a pattern anchored to one line sees the path without its
    # fragment and quietly drops `/` from the set.
    tree = ast.parse(mail)
    lines = mail.splitlines(keepends=True)
    targets = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = "".join(lines[node.lineno - 1 : node.end_lineno])
        if "click_url" not in body:
            continue
        targets.update(
            re.findall(
                r"public_base_url\.rstrip\('/'\)\}(/[a-z]*)(?:\"\s*\n\s*f?\")?#",
                body,
            )
        )
    assert targets, "no click_url targets found - has mail.py changed shape?"
    assert targets == {"/", "/confirm", "/manage"}, (
        f"the set of notification targets changed: {targets}. Every one of them needs to re-read "
        f"its fragment - see this test's docstring - so update the expectation deliberately."
    )

    for path in sorted(targets):
        # page_source: the listener may live in the page or in a script it loads.
        body = page_source(client, path)
        assert "addEventListener('hashchange'" in body, (
            f"{path} can be opened by a notification but never re-reads its fragment - a tab "
            f"already on {path} will silently ignore the token. See this test's docstring."
        )


def test_the_pages_module_constants_are_const_not_var(client):
    """The one guard against a bug this file has now shipped three times.

    Each was the same shape: something that runs during page setup read a module constant declared
    further down, `var` hoisted the declaration without the assignment, and the read returned
    `undefined` rather than throwing. They failed silently and differently - every visitor told
    their browser could not do push, every returning subscriber shown the signup form again, a
    stored range preference written correctly and never once read back. None was caught by a test;
    all three were found by driving a browser.

    `const` has a temporal dead zone, so the same mistake stops the page where it happens. Measured
    by making it: the range picker, legend and slider never appear, which the first person to load
    the page cannot miss.

    Asserted here rather than left to review because the failure mode is invisible in a diff - a
    `var` in the right place today is a bug the moment something above it grows a read.
    """
    source = client.get("/static/signup.js").text
    for name in ("CONFIG", "WINDOW_KEY", "VAPID_KEY", "EMAIL_AVAILABLE"):
        assert f"const {name} =" in source, f"{name} must be const - see this test's docstring"
        assert f"var {name} =" not in source, f"{name} went back to var"


@pytest.mark.parametrize(
    ("path", "hint"),
    [
        # "oben", because on the merged page the map is above the form rather than inside it,
        # and a hint pointing at a control the reader has scrolled past has to say where it is.
        ("/", "Tippe oben in die Karte, um deinen Ort zu setzen."),
        ("/manage", "Tippe in die Karte, um den Ort zu setzen."),
    ],
)
def test_the_map_hint_names_one_gesture_and_not_both(client, db, path, hint):
    """The thing being avoided is "Tippe oder klicke", which is clumsy and reads as a page unsure
    who it is talking to. Which single verb it picks is a platform judgement, and it moved: it was
    `Klicke` while the push channel was an app you installed on a phone from a desktop signup page,
    and it is `Tippe` now that the service is Android-first and every other instruction in these
    templates says *antippen*. Neither is right for both platforms; consistency within the page is
    what is achievable.
    """
    body = client.get(path).text
    assert hint in body
    assert "Tippe oder klicke" not in body


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

    page = page_source(client, "/manage")
    for threshold, rgba, label in INTENSITY_BANDS:
        assert f'value="{threshold}"' in page, f"{label} missing from the dropdown"
        assert f"rgba({rgba[0]},{rgba[1]},{rgba[2]}," in page
        assert label in page
    # Kept short: the option is a name and a threshold. The hourly equivalent is an
    # extrapolation that needs a sentence to be honest, and there is no room for one in a
    # dropdown - it stays on the map legend, where it is a tooltip.
    # The rendered page only: `page_source` would pull in radar.js, which contains "mm/h" in a
    # code path that formats a tooltip - a true statement about the source and a false one about
    # what the reader is shown. This assertion is about the reader.
    assert "mm/h" not in client.get("/manage").text
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
    for path in ("/", "/manage"):
        page = page_source(client, path)
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
    page = " ".join(page_source(client).split())  # the template wraps; the sentence does not
    assert (
        "Ich bin einverstanden, dass die Push-Adresse dieses Browsers und mein Standort "
        "gespeichert werden" in page
    )
    assert "Ich bin einverstanden, dass meine E-Mail-Adresse und mein Standort gespeichert" in page


def test_the_signup_note_does_not_claim_nothing_is_stored(client):
    """It was not true: `subscribe` writes a pending row before anyone confirms.

    The privacy page always said unconfirmed signups are deleted after a while, so the front
    page was contradicting it - in a consent notice, which is the worst place for it.
    """
    page = page_source(client)
    assert "Ohne Bestätigung wird nichts gespeichert" not in page
    assert "Stunden gelöscht" in page


def test_both_channel_wordings_are_in_the_page_source(client):
    """Rendered, not assembled by script - consent should be readable in the page itself."""
    page = page_source(client)
    assert page.count('class="for-push"') >= 2
    assert page.count('class="for-email"') >= 2


def test_the_stored_consent_record_names_the_channel_and_the_version(db, settings):
    """Two wordings share a version, so the channel is what disambiguates them."""
    from rainalert import subscriptions as svc
    from rainalert.db.models import Channel, Subscriber

    with db() as session:
        svc.subscribe(
            session,
            settings,
            lat=50.1,
            lon=8.6,
            channel=Channel.WEBPUSH,
            address="https://fcm.googleapis.com/fcm/send/abc",
            push_p256dh="k" * 87,
            push_auth="a" * 22,
        )
        row = session.query(Subscriber).one()
        assert row.channel is Channel.WEBPUSH
        assert row.consent_text_version == settings.consent_text_version


def test_the_privacy_page_covers_both_channels(client):
    page = client.get("/privacy").text
    assert "E-Mail-Adresse" in page
    # What a push subscriber is actually giving us, in their words rather than ours: the browser's
    # push address, and the fact that a third-party push service is in the path.
    assert "Push-Adresse" in page
    assert "Push-Dienst" in page


def test_the_settings_page_has_no_sign_out_control(client):
    """Subscribed or not: the only account controls are subscribing and unsubscribing
    (PLAN_DEVICE_KEY.md §11). With a device key a sign-out would do nothing - the next visit signs
    in again - and email sessions end on their own."""
    page = page_source(client, "/manage")
    assert 'id="logout"' not in page
    assert "Sitzung auf diesem Gerät beenden" not in page
    assert "/api/v1/manage/logout" not in page


def test_the_subscribe_page_links_to_the_settings_page(client):
    """Someone already subscribed lands on / looking for their settings."""
    assert 'href="/manage"' in page_source(client)


@pytest.mark.parametrize("path", ["/", "/manage"])
def test_both_maps_zoom_to_street_level(client, path):
    assert "maxZoom: 18" in page_source(client, path)


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
    """No page pulls an image from another origin.

    This used to be the narrower claim - scripts and styles came from unpkg, so only images could
    be held to 'self'. Since Leaflet was vendored (D-44) nothing does, and
    tests/test_vendored_leaflet.py asserts the general form. This stays as the specific one,
    because the marker icon is exactly where it was broken before.
    """
    for path in ("/", "/manage"):
        page = client.get(path).text
        for marker in ('<img src="https://', "src: 'https://", "iconUrl"):
            assert marker not in page, f"{path} pulls an image from elsewhere"


# --- attribution (DESIGN.md 4.2) ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/manage", "/privacy"])
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
    from types import SimpleNamespace

    from rainalert.api.mail import confirmation_message, deletion_receipt, manage_link_message
    from rainalert.db.models import Channel

    local = settings.model_copy(update={"public_base_url": "http://203.0.113.10:8000"})
    messages = [
        confirmation_message(local, "you@example.com", "T"),
        confirmation_message(
            local,
            "https://fcm.googleapis.com/fcm/send/abc",
            "T",
            channel="webpush",
            subscriber=SimpleNamespace(
                channel=Channel.WEBPUSH, push_p256dh="k" * 87, push_auth="a" * 22
            ),
        ),
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


# --- the settings button on a notification -----------------------------------------------------


def _push_settings(**kwargs):
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.invalid",
        mail_from="RainAlert <noreply@rain.example.invalid>",
        secret_key="test-secret",
        _env_file=None,
        **kwargs,
    )


def test_a_push_payload_carries_no_buttons():
    """Notifications carry no buttons (DESIGN.md D-64): a tap opens the page the message points at,
    and settings and unsubscribing live on the settings page only."""
    from rainalert.notify.webpush import payload_for

    message = OutboundMessage(
        to="https://fcm.googleapis.com/fcm/send/x",
        subject="s",
        text="t",
        channel="webpush",
        click_url="https://rain.example.invalid/manage",
        push_p256dh="k" * 87,
        push_auth="a" * 22,
    )
    assert "actions" not in json.loads(payload_for(message))


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
    assert "[hidden] { display: none !important; }" in page_source(client)


def test_the_picker_map_does_not_depend_on_the_radar(client):
    """`has_map` says whether there is imagery to lay over the map. Picking a place needs the
    basemap, not the radar - gating the whole map on it left the signup page with no way to
    choose a location at all on a fresh install."""
    body = page_source(client)
    assert "Engine.createMap(" in body and "Engine.picker(" in body
    # Choosing the engine must not consult the radar flag either - it decides whether there is a
    # map at all (D-58).
    choose = body[body.index("function chooseEngine()") :]
    assert "hasOverlay" not in choose[: choose.index("\n}\n")]

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
    body = page_source(client, "/manage")
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
    body = page_source(client)
    assert "außerhalb Deutschlands" in body
    # The same bounds the server enforces, so the page cannot drift from it.
    from rainalert.subscriptions import LAT_RANGE, LON_RANGE

    assert f"lat < {LAT_RANGE[0]:.0f} || lat > {LAT_RANGE[1]:.0f}" in body
    assert f"lon < {LON_RANGE[0]:.0f} || lon > {LON_RANGE[1]:.0f}" in body


# --- the signup result is different on each platform ---------------------------------------


def test_the_page_asks_for_permission_only_on_submit(client):
    """Asking before anyone has said what they want is how a site trains people to hit Block, and a
    blocked site cannot recover without the reader going into browser settings."""
    body = page_source(client)
    assert "Notification.requestPermission()" in body
    # The prompt lives in pushSubscription(), which the submit handler awaits. What matters is that
    # nothing calls it on load - a bare call at top level, or from a DOMContentLoaded handler, is
    # the shape that trains people to hit Block.
    handler = body.split("addEventListener('submit'")[1]
    assert "await pushSubscription()" in handler
    prologue = body.split("addEventListener('submit'")[0]
    assert "requestPermission()" in prologue.split("async function pushSubscription()")[1], (
        "requestPermission must be inside pushSubscription, not at module scope"
    )


def test_a_denied_permission_gets_its_own_wording(client):
    """Chrome treats a second call after a denial as already-denied and shows nothing, so there is
    no prompt left to answer - the reader has to undo it in the browser's own UI, and being told to
    "allow notifications" again would be advice they cannot follow."""
    body = page_source(client)
    # Two places check it now, and both must give the same instruction: `announceCapability()` at
    # load time, so a reader who blocked us last week is told before filling the form in, and the
    # submit path, for a denial that happens during this visit.
    checks = body.count("permission === 'denied'")
    assert checks >= 2, (
        "expected the denied state to be handled both at load time and on submit, "
        f"found {checks} check(s)"
    )
    # Every branch that mentions a denial must name the route out of it. Asserted per occurrence
    # rather than over a fixed-size slice of the page: the window version broke the moment a second
    # check was added above the first, which is a test reporting on its own brittleness rather than
    # on the page.
    for index, part in enumerate(body.split("permission === 'denied'")[1:], start=1):
        assert "Website-Einstellungen" in part[:900], (
            f"denied branch {index} does not tell the reader where to re-enable notifications"
        )
    # "Website-Einstellungen", not "Browser-Einstellungen (Schloss-Symbol)": the padlock is desktop
    # Chrome, and on Android - the target - there is no padlock to look for.
    assert "Schloss-Symbol" not in body, "the padlock does not exist on Android Chrome"


def test_the_page_says_what_an_iphone_needs(client):
    """Web push on iOS works only for a site added to the Home Screen, which is the one platform
    caveat a reader cannot discover for themselves - the API simply is not there."""
    body = page_source(client)
    assert "Home-Bildschirm" in body


def test_a_finished_signup_stops_being_a_form(client):
    """The button stayed live directly above the result, and pressing it again mints a second
    subscription with a second topic, silently replacing the one on screen. On a desktop that
    was the likely next move: the result began at y=868 of a 900-pixel viewport and the page did
    not scroll, so one press looked like nothing had happened."""
    body = page_source(client)
    assert "function settled(pending)" in body
    assert "document.getElementById('signup').hidden = true;" in body
    assert "scrollIntoView" in body
    # Both channels finish, not just push.
    assert body.count("settled(") >= 3
    # And there is a way back, because hiding the form removes the only one.
    assert "Von vorn anfangen" in body
    # The sentence after that link depends on whether anything is actually pending. It used to be
    # unconditional, so an already-confirmed browser re-signing up was told its subscription would
    # expire unless confirmed - and then sent back to the same branch by the link. A loop.
    assert "deine Anmeldung bleibt dabei bestehen" in body
    assert "settled(false)" in body


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
    )
    from rainalert.db.models import Channel

    who = _uuid.uuid4()
    subscriber = SimpleNamespace(id=who, address="a@b.example", channel=Channel.EMAIL)
    subscription = SimpleNamespace(timezone="Europe/Berlin")
    payload = {
        "predicted_start_at": "2026-09-23T14:30:00+00:00",
        "cycle_time": "2026-09-23T14:00:00+00:00",
        "lead_minutes": 30,
        "peak_mm_5min": 0.4,
    }
    return {
        "confirmation": (confirmation_message(settings, "a@b.example", "T"), False),
        "manage link": (manage_link_message(settings, "a@b.example", "T", who), True),
        "alert": (alert_message(None, settings, subscriber, subscription, payload), True),
        "deletion receipt": (deletion_receipt(settings, "a@b.example"), False),
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
    # The push receipt no longer asks the reader to do anything: the page or the service
    # worker releases the browser's own subscription, which only the subscriber could do while
    # the channel was an ntfy topic living in a separate app.
    assert "geloescht" in messages["deletion receipt"][0].text


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
EVERY_PAGE = ("/", "/manage", "/confirm", "/unsubscribe", "/privacy")


def test_no_shared_cache_may_keep_a_page_that_carries_a_nonce(client, db):
    """Every page here is per-request, and a shared cache must not pass one visitor's to another.

    Each response carries a fresh CSP nonce in both the header and the markup, and they are only
    useful as a pair: a cache handing one visitor's body to another either blocks every script on
    the page or hands out a nonce an injected inline script could claim. `/confirmed` is worse - it
    sets the session cookie, so its body belongs to one subscriber.

    Nothing sat in front of Cloud Run when these pages were written, so no page set this header at
    all. That stops being true the moment a CDN, a load balancer or Firebase Hosting is added for a
    custom domain, and it is not a change anyone would think to make at the same time.

    `private` is the load-bearing half. `no-cache` is revalidation, not "do not store", and
    `no-store` is deliberately *not* used: Chrome refuses the back/forward cache for a `no-store`
    document, which would make every Back into the start page rebuild the map and refetch the
    timeline instead of restoring instantly.
    """
    for path in EVERY_PAGE:
        cache = client.get(path).headers.get("Cache-Control", "")
        assert "private" in cache, f"{path} may be kept by a shared cache: {cache!r}"
        assert "no-cache" in cache, f"{path} is not revalidated: {cache!r}"
        assert "no-store" not in cache, (
            f"{path} sets no-store, which costs the back/forward cache - see this test's docstring"
        )


def test_every_page_carries_the_same_navigation(client, db):
    for path in EVERY_PAGE:
        body = client.get(path).text
        nav = body.split('<nav class="site"')[1].split("</nav>")[0]
        for label in ("Start", "Einstellungen"):
            assert label in nav, f"{path} is missing {label}"


def test_the_navigation_sits_between_the_content_and_the_footer(client, db):
    """Always in the same place, so it is found by habit rather than by looking."""
    for path in EVERY_PAGE:
        body = client.get(path).text
        assert body.index('<nav class="site"') < body.index("<footer>"), path


def test_the_page_you_are_on_is_marked_and_is_not_a_link(client, db):
    """The set keeps its shape as you move around - the current entry is marked, not dropped."""
    import re

    for path, label in (("/", "Start"), ("/manage", "Einstellungen")):
        nav = client.get(path).text.split('<nav class="site"')[1].split("</nav>")[0]
        # Whitespace-insensitive: the assertion is about which element wraps the label, not
        # about how Jinja happened to indent it.
        current = re.search(r'<strong aria-current="page">\s*([^<\s]+)', nav)
        assert current and current.group(1) == label, (
            f"{path}: marked {current and current.group(1)}"
        )
        assert f'href="{path}"' not in nav, f"{path} links to itself"


def test_the_navigation_does_not_depend_on_there_being_radar_imagery(client, db):
    """`has_map` says whether there is imagery to lay over the map, not whether the map exists:
    the page renders its graticule and its "no radar data yet" banner perfectly well without an
    overlay store. Gating a navigation entry on it once left the map missing from the navigation
    while the reader was standing on it, and nothing here may reintroduce that.

    The entry itself is gone - the radar and the signup form are one page now, so a second
    `Regenradar` link would be `Start` under another name - but the rule outlived it.
    """
    # This client has no overlay store configured, which is the case under test.
    assert "Noch keine Radardaten" in client.get("/static/radar.js").text
    for path in EVERY_PAGE:
        nav = client.get(path).text.split('<nav class="site"')[1].split("</nav>")[0]
        assert "Start" in nav and "Einstellungen" in nav, path
    # And the map is still on the start page, overlay store or not.
    assert 'id="map"' in client.get("/").text


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


def test_the_confirm_button_is_not_in_the_page_until_it_is_needed(client):
    """A push confirmation confirms on open, so its button is a thing to press that is already
    being pressed - and it appeared for a moment on every one of them, which is exactly long
    enough to reach for.

    Asserted on the served markup rather than in a browser because that is where the guarantee
    lives: `hidden` plus base.html's `[hidden] { display: none !important }` cannot be painted
    before the script runs, whereas hiding it *from* the script always can be. The script runs
    after this markup is parsed, so by then the button may already be on screen.
    """
    body = client.get("/confirm").text
    form = body[body.index("<form") : body.index("</form>")]
    assert "hidden" in form.split(">")[0], form.split(">")[0]
    # Revealed on the mailed path only - that one does wait for a human (F-4).
    assert "form.hidden = false" in body
    # And there is something to look at while the push path works.
    assert 'id="busy"' in body and "spinner" in body


def test_the_confirm_page_says_why_nothing_happens_without_script(client):
    """The code is in the fragment, so no script means no confirmation - and until now the page
    just sat there. A reader who sees nothing happen deserves the reason."""
    body = client.get("/confirm").text
    assert "<noscript>" in body
    note = body[body.index("<noscript>") : body.index("</noscript>")]
    assert "JavaScript" in note


def test_a_subscription_made_with_another_vapid_key_is_not_reused(client):
    """A push subscription is bound to the `applicationServerKey` it was created with. Reuse one
    made under a different key and every send is rejected 403 by the push service - permanently,
    and invisibly from the reader's side, because their signup succeeded. So the page compares
    before reusing, and replaces on a mismatch.

    Asserted on the served page rather than in a unit test because there is no unit to test: this
    is browser glue, and the last time glue like it broke (a reference to a deleted constant) every
    Python test still passed while signup was dead. The behaviour itself is exercised by driving the
    function - see the scratchpad harness in the notes for this change.
    """
    body = page_source(client)
    assert "function sameKey(" in body
    # Reuse is conditional, and the mismatch path actually drops the old subscription.
    assert "if (sameKey(existing, VAPID_KEY)) { return existing; }" in body
    assert "existing.unsubscribe();" in body
    # Compared as bytes, because options.applicationServerKey is an ArrayBuffer and the page holds
    # base64url. Asserted on the conversion rather than on the exact expression: pinning
    # `new Uint8Array(options.applicationServerKey)` failed when the value was hoisted into a local
    # to add the ArrayBuffer guard, which is a rename rather than a regression. What the comparison
    # actually decides, guard included, is asserted by running it - tests/js/page_test.mjs.
    assert "new Uint8Array(" in body.split("function sameKey(")[1]


# --- the manifest, which iOS web push does not work without ------------------------------------


def test_every_page_links_the_manifest(client):
    """Without a linked manifest iOS has no web push at all - `PushManager` simply is not there for
    a page that is not an installed web app. The link was missing from `base.html` entirely, which
    made the iPhone half of D-45 impossible, and nothing failed."""
    for path in ("/", "/manage", "/privacy"):
        body = client.get(path).text
        assert 'rel="manifest"' in body, f"{path} does not link the manifest"


def test_the_manifest_says_what_ios_requires(client):
    """`display: standalone` is the field that makes iOS treat this as an installable web app, and
    therefore the field web push on iOS depends on. Delete that one string and the iPhone flow dies
    silently - so it is asserted by value, not by presence."""
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert "application/manifest+json" in response.headers["content-type"]
    manifest = response.json()
    assert manifest["display"] == "standalone"
    # Scope has to cover the worker's scope, or an installed instance opens outside it.
    assert manifest["scope"] == "/"
    assert manifest["start_url"]


def test_the_manifest_is_named_in_german(client):
    """`short_name` is what Android prints above every notification and what an installed icon is
    labelled with. It was "RainAlert" - the repository's name, in English, on a German-only site, so
    a reader who signed up at "Regenwarnung" would have got notifications from something else."""
    manifest = client.get("/manifest.webmanifest").json()
    assert "RainAlert" not in manifest["short_name"]
    assert "RainAlert" not in manifest["name"]
    assert "Regenwarnung" in manifest["short_name"]


def test_every_icon_the_manifest_names_is_served(client):
    """A manifest naming an icon that 404s is an install prompt with a broken image, and on Android
    a notification with Chrome's default icon instead of ours."""
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["icons"], "the manifest must name at least one icon"
    for icon in manifest["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200, f"{icon['src']} is named by the manifest but 404s"
        assert response.headers["content-type"].startswith("image/")
    # A maskable icon is separate from the `any` one: Android crops a non-maskable icon into a
    # circle and eats the edges of the artwork.
    purposes = {icon.get("purpose") for icon in manifest["icons"]}
    assert "maskable" in purposes


def test_both_pages_check_the_vapid_key_before_reusing_a_subscription(client):
    """A subscription is bound to the `applicationServerKey` it was made with, and a push signed
    with any other key is refused 403 forever - invisibly, from the reader's side.

    Both routes that read an existing subscription have to check: `/` before reusing one to sign up,
    and `/manage` before offering to send a settings link to one. The signup page's comparison is
    exercised by driving it (tests/js/page_test.mjs); this asserts that neither page has lost the
    check, which is the failure mode that would otherwise be silent on both.
    """
    assert "function sameKey(" in page_source(client)
    assert "function usesOurKey(" in page_source(client, "/manage")


@pytest.mark.parametrize("path", ["/manage/", "/privacy/", "/api/v1/subscriptions/me/"])
def test_a_trailing_slash_is_not_redirected(client, path):
    """Behind Firebase Hosting a redirect's `Location` named the internal run.app host over http."""
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 404
    assert "location" not in response.headers


def test_a_confirmation_does_not_depend_on_its_notification_being_clicked(client):
    """D-65: on desktop Chrome on a Mac the click can reach nothing - Chrome had already dropped the
    notification macOS still showed. The worker hands the link to the start page, which confirms by
    itself; the confirmed page forgets the link and closes the notification. The behaviour is run in
    Chromium (a real worker, a real push via DevTools) and in tests/js/sw_test.mjs; this pins the
    wiring."""
    start = client.get("/").text
    assert start.index("pendingconfirm.js") < start.index("signup.js")
    confirmed_src = Path(__file__).resolve().parents[1] / "rainalert" / "api" / "templates"
    assert "RainPending.clear()" in (confirmed_src / "confirmed.html").read_text(encoding="utf-8")
    signup = (
        Path(__file__).resolve().parents[1] / "rainalert" / "api" / "static" / "signup.js"
    ).read_text(encoding="utf-8")
    assert "window.RainPending.listen(confirmIfHandedOver)" in signup
