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
    assert body["attribution"].startswith("Deutscher Wetterdienst")
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
