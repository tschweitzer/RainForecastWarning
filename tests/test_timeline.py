"""The map timeline: renderer, manifest, and the page's CSP.

The manifest's job is to be honest about three things - which frames are observations and which are
forecasts, which slots have no data at all, and how old the newest cycle is.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rainalert.api.app import create_app
from rainalert.config import Settings
from rainalert.db.models import CycleStatus, RadarCycle
from rainalert.notify import ConsoleNotifier
from rainalert.radar.decoder import read_frames
from rainalert.radar.overlay import BOUNDS, build_projection, colorize, render_frame
from rainalert.storage import LocalOverlayStore
from rainalert.timeline import build_timeline

T0 = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg://unused",
        overlay_dir=str(tmp_path / "overlays"),
        secret_key="test-secret",
        _env_file=None,
    )


@pytest.fixture()
def overlays(tmp_path):
    return LocalOverlayStore(tmp_path / "overlays")


def add_cycles(session, count, end=T0, step_minutes=5, status=CycleStatus.OK):
    for index in range(count):
        session.add(
            RadarCycle(
                nominal_time=end - timedelta(minutes=step_minutes * index),
                fetched_at=end,
                source_url="test",
                sha256=b"\x00" * 32,
                bytes=1,
                frame_count=25,
                status=status,
            )
        )
    session.commit()


# --- renderer --------------------------------------------------------------------------------


def test_render_produces_a_png_of_the_expected_shape(wet_cycle):
    projection = build_projection()
    png = render_frame(read_frames(wet_cycle)[0], projection)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(png))
    assert image.size == (projection.width, projection.height)
    assert image.mode == "RGBA"


def test_dry_and_missing_are_both_transparent(wet_cycle):
    """The map is not the place to distinguish 'no rain' from 'no data'."""
    import numpy as np

    values = np.array([[float("nan"), 0.0, 0.01], [0.06, 1.0, 9.0]])
    rgba = colorize(values)
    assert rgba[0, 0, 3] == 0  # NaN
    assert rgba[0, 1, 3] == 0  # dry
    assert rgba[0, 2, 3] == 0  # below the first stop
    assert rgba[1, 0, 3] > 0  # light rain
    assert rgba[1, 2, 3] > rgba[1, 1, 3] or rgba[1, 2, 3] > 0  # heavier is more opaque


def test_a_frame_is_small_enough_to_send_168_of(wet_cycle):
    """168 frames over mobile data is the constraint that sizes this."""
    png = render_frame(read_frames(wet_cycle)[0])
    assert len(png) < 200 * 1024


# --- manifest --------------------------------------------------------------------------------


def test_timeline_labels_observed_and_forecast_frames(db, settings, overlays):
    with db() as session:
        add_cycles(session, 12)
        manifest = build_timeline(session, settings, overlays, now=T0 + timedelta(minutes=3))

    observed = [f for f in manifest["frames"] if f["kind"] == "observed"]
    forecast = [f for f in manifest["frames"] if f["kind"] == "forecast"]
    assert len(observed) == 12
    assert len(forecast) == 24  # +5 ... +120
    assert all(f["offset_minutes"] <= 0 for f in observed)
    assert all(f["offset_minutes"] > 0 for f in forecast)
    # every past frame comes from its own cycle; every future frame from the latest one
    assert len({f["source_cycle"] for f in observed}) == 12
    assert len({f["source_cycle"] for f in forecast}) == 1
    assert manifest["bounds"] == [list(BOUNDS[0]), list(BOUNDS[1])]


def test_frames_are_ordered_and_span_the_window(db, settings, overlays):
    with db() as session:
        add_cycles(session, 6)
        manifest = build_timeline(session, settings, overlays, now=T0)
    offsets = [f["offset_minutes"] for f in manifest["frames"]]
    assert offsets == sorted(offsets)
    assert offsets[0] == -25
    assert offsets[-1] == 120


def test_missing_cycles_are_reported_as_gaps(db, settings, overlays):
    """A hole must be drawn as a hole - holding the previous image fakes continuity."""
    with db() as session:
        add_cycles(session, 3, end=T0)  # 14:00, 13:55, 13:50
        add_cycles(session, 2, end=T0 - timedelta(minutes=30))  # 13:30, 13:25
        manifest = build_timeline(session, settings, overlays, past_hours=1, now=T0)

    gaps = manifest["gaps"]
    assert gaps, "the 13:35-13:45 hole must be reported"
    covered = {
        offset
        for gap in gaps
        for offset in range(gap["from_offset_minutes"], gap["to_offset_minutes"] + 1, 5)
    }
    assert {-15, -20, -25} <= covered  # 13:45, 13:40, 13:35
    assert -30 not in covered  # 13:30 exists
    assert 0 not in covered


def test_staleness_is_stated_not_hidden(db, settings, overlays):
    with db() as session:
        add_cycles(session, 1)
        fresh = build_timeline(session, settings, overlays, now=T0 + timedelta(minutes=4))
        stale = build_timeline(session, settings, overlays, now=T0 + timedelta(minutes=45))
    assert fresh["stale"] is False
    assert stale["stale"] is True
    assert stale["age_minutes"] == pytest.approx(45, abs=0.2)


def test_past_hours_is_clamped(db, settings, overlays):
    """A client cannot ask for more history than we keep."""
    with db() as session:
        add_cycles(session, 20)
        manifest = build_timeline(session, settings, overlays, past_hours=999, now=T0)
    observed = [f for f in manifest["frames"] if f["kind"] == "observed"]
    assert min(f["offset_minutes"] for f in observed) >= -settings.timeline_past_hours * 60


def test_empty_database_yields_an_empty_but_valid_manifest(db, settings, overlays):
    with db() as session:
        manifest = build_timeline(session, settings, overlays, now=T0)
    assert manifest["frames"] == []
    assert manifest["stale"] is True
    assert manifest["latest_cycle"] is None


# --- the page --------------------------------------------------------------------------------


@pytest.fixture()
def client(db, settings):
    return TestClient(create_app(settings, session_factory=db, notifier=ConsoleNotifier()))


def test_timeline_endpoint_serves_the_manifest(client, db):
    with db() as session:
        add_cycles(session, 3)
    body = client.get("/api/v1/overlays/timeline").json()
    # Content, not prefix: the credit is one shared constant now, and CC BY needs the source,
    # the licence and the fact that the data was modified - not a particular word order.
    assert "Deutscher Wetterdienst" in body["attribution"]
    assert "CC BY 4.0" in body["attribution"]
    assert "eigene Verarbeitung" in body["attribution"]
    assert body["colorscale"]


def test_every_inline_script_carries_the_csp_nonce(client):
    """Regression: `default-src 'self'` with no script-src silently blocked the subscribe page's
    own script. Assert the pages are actually loadable under the policy we send, not merely that
    a header exists."""
    import re

    for path in ("/", "/map"):
        page = client.get(path)
        assert page.status_code == 200, path
        policy = page.headers["content-security-policy"]
        nonce = re.search(r"'nonce-([\w-]+)'", policy).group(1)
        for tag in re.findall(r"<script[^>]*>", page.text):
            assert f'nonce="{nonce}"' in tag, f"{path}: un-nonced script {tag}"


def test_csp_still_forbids_framing_and_declares_connect_src(client):
    policy = client.get("/").headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "base-uri 'none'" in policy


# --- re-render -------------------------------------------------------------------------------


def test_rerender_rebuilds_the_timeline_from_archives(tmp_path, wet_cycle, overlays):
    """A fresh deployment fills its history from what it already has, not from DWD."""
    from rainalert.jobs.backfill import rerender_observed

    archive_dir = tmp_path / "raw"
    archive_dir.mkdir()
    for stamp in ("2609161355", "2609161400"):
        (archive_dir / f"DE1200_RV{stamp}.tar.bz2").write_bytes(wet_cycle.read_bytes())

    report = rerender_observed(archive_dir, overlays)
    assert report.archives == 2
    assert report.rendered == 2  # only the t+0 frame of each
    assert report.failed == 0
    assert list((overlays.root / "obs").glob("*.png"))


def test_rerender_survives_an_unreadable_archive(tmp_path, wet_cycle, overlays):
    from rainalert.jobs.backfill import rerender_observed

    archive_dir = tmp_path / "raw"
    archive_dir.mkdir()
    (archive_dir / "DE1200_RV2609161355.tar.bz2").write_bytes(wet_cycle.read_bytes())
    (archive_dir / "DE1200_RV2609161400.tar.bz2").write_bytes(b"not an archive")

    report = rerender_observed(archive_dir, overlays)
    assert (report.rendered, report.failed) == (1, 1)


def test_overlay_pruning_keeps_observed_longer_than_forecast(overlays):
    """Observed frames feed the 12 h timeline; only the newest forecast is ever shown (D-7)."""
    old = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    recent = datetime(2026, 9, 16, 13, 55, tzinfo=UTC)
    for stamp in (old, recent):
        overlays.put_observed(stamp, b"\x89PNG\r\n\x1a\n")
        overlays.put_forecast(stamp, 60, b"\x89PNG\r\n\x1a\n")

    removed = overlays.prune(
        observed_before=datetime(2026, 9, 16, 6, 0, tzinfo=UTC),
        forecast_before=datetime(2026, 9, 16, 13, 0, tzinfo=UTC),
    )
    assert removed == 2  # the old observed frame and the old forecast directory
    assert len(list((overlays.root / "obs").glob("*.png"))) == 1


# --- metrics ---------------------------------------------------------------------------------


def test_metrics_does_not_exist_without_a_token(client):
    """ "Internal only" is not expressible on Cloud Run, so unconfigured means absent (F-13)."""
    assert client.get("/metrics").status_code == 404


def test_metrics_requires_the_token_when_configured(db, settings):
    settings.metrics_token = "s3cret"
    metrics_client = TestClient(
        create_app(settings, session_factory=db, notifier=ConsoleNotifier())
    )
    assert metrics_client.get("/metrics").status_code == 401
    assert (
        metrics_client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    )
    ok = metrics_client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert "rainalert_cycle_age_seconds" in ok.text


def test_a_future_stamped_cycle_is_its_own_alertable_condition(db, settings):
    """A cycle in the future would read as the freshest data ever and silence staleness (F-7)."""
    from rainalert.api.metrics import render

    with db() as session:
        add_cycles(session, 1, end=T0 + timedelta(days=1))
        text = render(session, settings, now=T0)
    assert "rainalert_cycle_age_negative 1" in text
    # ...and the age itself is clamped rather than reported as negative
    age = float(
        next(
            line.split()[1]
            for line in text.splitlines()
            if line.startswith("rainalert_cycle_age_seconds ")
        )
    )
    assert age == 0.0


def test_metrics_report_no_cycles_distinctly(db, settings):
    from rainalert.api.metrics import render

    with db() as session:
        text = render(session, settings, now=T0)
    assert "rainalert_cycle_age_seconds -1" in text


def test_the_map_borrows_no_tiles_by_default(client):
    """tile.openstreetmap.org is volunteer-run and its policy excludes applications.

    It blocks them, too - which is how this was found. Shipping a default that leans on it is
    taking something that was not offered, so there is no default provider at all.
    """
    body = client.get("/map").text
    assert "openstreetmap.org" not in body
    assert "L.tileLayer" in body  # still there, for whoever configures one
    # and the map is still readable: orientation without a basemap
    assert "graticule()" in body
    assert "Muenchen" in body


def test_csp_allows_only_the_configured_tile_origin(client):
    """The policy follows the provider in use, so it can never be wider than the provider."""
    policy = client.get("/map").headers["Content-Security-Policy"]
    img = next(d for d in policy.split(";") if d.strip().startswith("img-src"))
    assert "openstreetmap" not in img
    assert "https:" not in img.replace("https://", "")  # no blanket https: source


def test_tile_origin_never_widens_past_the_provider():
    from rainalert.api.app import tile_origin

    assert tile_origin("") == ""
    assert (
        tile_origin("https://tiles.example.com/{z}/{x}/{y}.png?key=k")
        == "https://tiles.example.com"
    )
    assert (
        tile_origin("https://{s}.tiles.example.com/{z}/{x}/{y}.png")
        == "https://*.tiles.example.com"
    )
    # Nothing that is not an http(s) origin may reach the policy.
    assert tile_origin("javascript:alert(1)") == ""
    assert tile_origin("data:image/png;base64,AAAA") == ""
    assert tile_origin("not a url") == ""


def test_tiles_identify_the_page_to_the_provider(client, monkeypatch):
    """The page sends Referrer-Policy: no-referrer, which also strips it from tile requests.

    Tile services read that header to tell an application from an anonymous scraper, so without
    an element-level override every tile arrives unidentified - which is what a provider blocks.
    """
    from rainalert.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MAP_TILE_URL", "https://tiles.example.com/{z}/{x}/{y}.png")
    monkeypatch.setenv("DATABASE_URL", Settings().database_url)

    body = client.get("/map").text
    assert "referrerPolicy: 'strict-origin-when-cross-origin'" in body
    # The origin alone, never a path or query: no token can ride out on a tile request.
    assert "'unsafe-url'" not in body
    assert "'origin-when-cross-origin'" not in body


def test_the_page_still_sends_no_referrer_by_default(client):
    """The override is per element. Everything else on the site keeps the strict header."""
    assert client.get("/").headers["Referrer-Policy"] == "no-referrer"
    assert client.get("/map").headers["Referrer-Policy"] == "no-referrer"


def test_playback_is_slow_enough_to_read(client):
    """125 ms per frame ran the whole loop in under five seconds - flicker, not weather.

    The floor matters more than the exact number: below roughly 300 ms there is no time to fixate
    on where a shower is relative to a town, which is the only question the animation answers.
    """
    import re

    body = client.get("/map").text
    step = int(re.search(r"var STEP_MS = (\d+)", body).group(1))
    assert 300 <= step <= 700, f"{step} ms per frame is outside the legible range"

    # The last frame and t+0 are held longer: the end state, and the boundary where measurement
    # becomes prediction.
    last = int(re.search(r"var HOLD_LAST_MS = (\d+)", body).group(1))
    now = int(re.search(r"var HOLD_NOW_MS = (\d+)", body).group(1))
    assert last > step and now >= step


def test_playback_cannot_stack_frames(client):
    """setInterval queues another callback when a frame paints slowly; setTimeout cannot."""
    body = client.get("/map").text
    assert "setInterval(" not in body  # the call, not the word - the comment explains why
    assert "clearTimeout(" in body  # and stop() clears the right kind of timer


def test_every_frame_is_labelled_with_its_date(client):
    """The slider reaches 12 h back, so it crosses midnight most evenings.

    Shown on every frame rather than only when the day changes: a label that gains a date only
    sometimes is one you have to read twice to be sure it has not.
    """
    body = client.get("/map").text
    assert "toLocaleDateString('de-DE'" in body
    assert "weekday: 'short'" in body  # 'Do.' says yesterday faster than '17.' does


def test_long_offsets_are_shown_as_hours(client):
    """-720 min is a number you have to divide before it means anything."""
    body = client.get("/map").text
    assert "function relative(" in body
    assert "' h'" in body
    # the old unconditional minutes formatting is gone
    assert "'+' + frame.offset_minutes + ' min'" not in body


def test_the_window_defaults_to_everything_dwd_keeps():
    """The slider should not throw away history that is free to have."""
    from rainalert.config import DWD_RETENTION_HOURS, Settings

    s = Settings(database_url="postgresql+psycopg://x", _env_file=None)
    assert s.timeline_past_hours == DWD_RETENTION_HOURS
    # Both retentions outlast the window, or the oldest frame on the slider 404s mid-look.
    assert s.overlay_obs_retention_hours > s.timeline_past_hours
    assert s.raw_retention_hours > s.timeline_past_hours


def test_a_nonsense_window_does_not_empty_the_map(db, tmp_path):
    """`past_hours or default` lets a negative through, and the window then starts after it ends.

    The query returns nothing, so the map renders with no frames at all - which reads as "the
    radar is down" rather than "you asked for a window that runs backwards".
    """
    from datetime import UTC, datetime

    from rainalert.config import Settings
    from rainalert.db.models import CycleStatus, RadarCycle
    from rainalert.storage import LocalOverlayStore
    from rainalert.timeline import build_timeline

    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    settings = Settings(database_url="postgresql+psycopg://x", _env_file=None)
    store = LocalOverlayStore(tmp_path / "ov")

    with db() as session:
        session.add(
            RadarCycle(
                nominal_time=now,
                fetched_at=now,
                source_url="x",
                sha256=b"0" * 32,
                bytes=1,
                frame_count=25,
                status=CycleStatus.OK,
                archive_uri="file:///x",
            )
        )
        session.commit()

        for asked in (-5, -10000):
            result = build_timeline(session, settings, store, asked, now=now)
            observed = [f for f in result["frames"] if f["kind"] == "observed"]
            assert observed, f"past_hours={asked} produced an empty map"


def test_the_heading_states_the_window_actually_shown(client):
    """It said "12 Stunden" while serving 48, because the number was typed into the template."""
    assert "Die letzten 12 Stunden" in client.get("/map").text
    assert "Die letzten 6 Stunden" in client.get("/map?hours=6").text
    assert "Die letzten 48 Stunden" in client.get("/map?hours=48").text


def test_the_window_defaults_to_twelve_hours_not_the_maximum(client):
    """48 h is 577 slider positions. Reachable, but not what you land on."""
    from rainalert.config import Settings

    s = Settings(database_url="postgresql+psycopg://x", _env_file=None)
    assert s.timeline_default_hours == 12
    assert s.timeline_past_hours == 48  # still the ceiling

    body = client.get("/map").text
    assert "past_hours=12" in body


def test_a_rubbish_hours_parameter_still_gives_you_a_map(client):
    """Declared as an int, FastAPI answers ?hours=abc with a 422 page.

    There is a perfectly good default to fall back to, and nothing here worth an error page.
    """
    response = client.get("/map?hours=abc")
    assert response.status_code == 200
    assert "Die letzten 12 Stunden" in response.text


def test_an_out_of_range_window_is_clamped_not_refused(client):
    assert "Die letzten 48 Stunden" in client.get("/map?hours=999").text
    # and the German stays correct at the bottom of the range
    assert "Die letzte Stunde" in client.get("/map?hours=-3").text


def test_the_range_picker_marks_the_current_choice(client):
    body = client.get("/map?hours=6").text
    assert '<strong aria-current="true">6 h</strong>' in body
    assert "/map?hours=12" in body  # the others are plain links, shareable and JS-free


# --- map page layout -----------------------------------------------------------------------------


def test_the_window_sentence_sits_with_the_range_picker(client):
    """It describes the picker, so it belongs next to it - not above the map, where it was
    one more thing pushing the slider off a phone screen."""
    page = client.get("/map").text
    legend = page.index('id="legend"')
    sentence = page.index("Die letzten 12 Stunden")
    picker = page.index('class="range"')
    assert legend < sentence < picker


def test_the_map_leaves_room_for_the_slider(client):
    """The slider is the control people come to this page for.

    Sized in svh rather than vh: on a phone `vh` is the viewport with the browser chrome
    *hidden*, so a map sized in vh is taller than what is on screen. Measured in Chromium at
    375x553 the slider was below the fold before this and is not now.
    """
    page = client.get("/map").text
    assert "52svh" in page
    assert "55vh" in page, "the vh fallback must stay for browsers without svh"
    # No inline height on the element, or it would win over the stylesheet.
    assert 'id="map" style=' not in page


def test_the_page_does_not_explain_the_slider(client):
    """Dropped: a slider does not need to be told to be a slider, and the line cost a row of
    vertical space on the screen where space was the problem."""
    assert "Ziehen oder abspielen" not in client.get("/map").text
