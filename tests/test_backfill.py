"""Fetching past cycles from DWD (DESIGN.md 11.1).

This is the only place in the service that makes many requests in a row, so the tests are mostly
about restraint: how fast it goes, when it stops, and what it refuses to act on.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from rainalert.config import Settings
from rainalert.db.models import CycleStatus, RadarCycle
from rainalert.jobs.backfill import (
    JITTER_MAX_SECONDS,
    JITTER_MIN_SECONDS,
    fetch_missing,
    missing_cycles,
)
from rainalert.radar.client import ArchiveNotFound, archive_name
from rainalert.storage import LocalArchiveStore
from tests.helpers import FIXTURES, Recorder, make_client

WET = FIXTURES / "DE1200_RV2609161355_trimmed.tar.bz2"
NOMINAL = datetime(2026, 9, 16, 13, 55, tzinfo=UTC)


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg://unused",
        archive_dir=str(tmp_path / "raw"),
        expected_frame_count=3,  # the fixture is a trimmed cycle
        _env_file=None,
    )


def test_archive_name_matches_the_published_format():
    """DWD_RV_FORMAT.md 1: two-digit year, no separators."""
    assert archive_name(datetime(2026, 9, 18, 14, 35, tzinfo=UTC)) == "DE1200_RV2609181435.tar.bz2"


def test_the_window_lands_on_the_five_minute_grid(db):
    """`anchor - 0.1 h` is 13:49, and DWD has never published a cycle at 13:49.

    Every slot walked from an unaligned start is a file name that does not exist, so a window
    that is not a whole number of cycles would spend the entire run collecting 404s.
    """
    now = datetime(2026, 9, 16, 13, 57, tzinfo=UTC)
    with db() as session:
        for hours in (0.1, 0.25, 1.0, 12.0):
            wanted = missing_cycles(session, hours=hours, now=now)
            assert all(slot.minute % 5 == 0 for slot in wanted), hours
            assert wanted[-1] == datetime(2026, 9, 16, 13, 55, tzinfo=UTC)


def test_missing_cycles_lists_every_empty_slot_oldest_first(db):
    now = datetime(2026, 9, 18, 12, 2, tzinfo=UTC)  # mid-slot: must floor to 12:00
    with db() as session:
        session.add(
            RadarCycle(
                nominal_time=datetime(2026, 9, 18, 11, 50, tzinfo=UTC),
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

        wanted = missing_cycles(session, hours=1, now=now)

    assert wanted[0] == datetime(2026, 9, 18, 11, 0, tzinfo=UTC)
    assert wanted[-1] == datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    assert datetime(2026, 9, 18, 11, 50, tzinfo=UTC) not in wanted  # already held
    assert len(wanted) == 12  # 13 slots in the hour, one of them already held


def _blob() -> bytes:
    return WET.read_bytes()


def test_fetch_missing_stores_a_cycle_and_pauses_between_requests(db, settings, tmp_path):
    """Two downloads, one pause - and the pause is inside the jitter band."""
    rec = Recorder(*[httpx.Response(200, content=_blob()) for _ in range(2)])
    client = make_client(rec, max_response_bytes=8 * 1024 * 1024)
    slept: list[float] = []

    with db() as session:
        report = fetch_missing(
            session,
            client,
            LocalArchiveStore(tmp_path / "raw"),
            settings,
            hours=5 / 60,  # slots 13:50 and 13:55, given the `now` below
            now=NOMINAL,
            sleep=slept.append,
            limit=2,
        )

    # One pause between two requests, never before the first.
    assert len(slept) == 1
    assert JITTER_MIN_SECONDS <= slept[0] <= JITTER_MAX_SECONDS
    # The fixture is stamped 13:55, so only the slot that asked for 13:55 is accepted.
    assert report.fetched == 1
    assert report.rejected == 1


def test_the_default_pause_is_the_jittered_band(db, settings, tmp_path):
    """The delay is drawn per request, not fixed - two runs must not walk in lockstep."""
    draws = set()
    for _ in range(40):
        rec = Recorder(*[httpx.Response(200, content=_blob()) for _ in range(3)])
        slept: list[float] = []
        with db() as session:
            fetch_missing(
                session,
                make_client(rec, max_response_bytes=8 * 1024 * 1024),
                LocalArchiveStore(tmp_path / "raw"),
                settings,
                hours=15 / 60,
                now=NOMINAL,
                sleep=slept.append,
                limit=2,
            )
        draws.update(slept)
    assert len(draws) > 1, "the pause is constant; it is supposed to be jittered"
    assert all(JITTER_MIN_SECONDS <= d <= JITTER_MAX_SECONDS for d in draws)


def test_a_cycle_dwd_no_longer_keeps_is_not_an_error(db, settings, tmp_path):
    """404 means past the retention window. Counted, skipped, and never retried."""
    rec = Recorder(*[httpx.Response(404) for _ in range(3)])
    client = make_client(rec)

    with db() as session:
        report = fetch_missing(
            session,
            client,
            LocalArchiveStore(tmp_path / "raw"),
            settings,
            hours=1,
            limit=3,
            now=NOMINAL,
            sleep=lambda _s: None,
        )

    assert report.not_retained == 3
    assert report.fetched == 0
    # Three slots, three requests: a 404 must not be retried five times.
    assert len(rec.requests) == 3


def test_a_file_whose_header_disagrees_with_its_name_is_refused(db, settings, tmp_path):
    """We asked for one cycle and were handed another. Ingest cannot check this; here we can."""
    rec = Recorder(*[httpx.Response(200, content=_blob()) for _ in range(3)])

    with db() as session:
        report = fetch_missing(
            session,
            make_client(rec, max_response_bytes=8 * 1024 * 1024),
            LocalArchiveStore(tmp_path / "raw"),
            settings,
            hours=1,  # every slot but 13:55 gets a file stamped 13:55
            now=NOMINAL,
            sleep=lambda _s: None,
            limit=3,
        )

    assert report.fetched == 0
    assert report.rejected == 3

    with db() as session:
        assert session.execute(select(RadarCycle)).scalars().all() == []


def test_backfill_stops_when_the_budget_is_gone(db, settings, tmp_path):
    """Halting is reported, not pushed through."""
    rec = Recorder(*[httpx.Response(200, content=_blob()) for _ in range(20)])
    client = make_client(
        rec, max_response_bytes=8 * 1024 * 1024, hourly_byte_budget=len(_blob()) + 1
    )

    with db() as session:
        report = fetch_missing(
            session,
            client,
            LocalArchiveStore(tmp_path / "raw"),
            settings,
            hours=1,
            now=NOMINAL,
            sleep=lambda _s: None,
        )

    assert report.halted is not None
    assert len(rec.requests) < 13  # it gave up rather than walking the whole window


def test_backfill_never_sends_mail(db, settings, tmp_path):
    """History must not be warned about.

    Evaluating a backfilled cycle would mail everyone about rain that stopped hours ago, once per
    cycle. fetch_missing takes no notifier at all, which is the cheapest way to guarantee it.
    """
    import inspect

    assert "notifier" not in inspect.signature(fetch_missing).parameters


def test_stale_cycles_are_still_refused_outside_the_asked_for_window(db, settings, tmp_path):
    """The age check is widened to the window, not switched off."""
    rec = Recorder(*[httpx.Response(200, content=_blob()) for _ in range(3)])
    # "now" is three days after the fixture's stamp, so even a 12 h backfill must refuse it.
    later = NOMINAL + timedelta(days=3)

    with db() as session:
        report = fetch_missing(
            session,
            make_client(rec, max_response_bytes=8 * 1024 * 1024),
            LocalArchiveStore(tmp_path / "raw"),
            settings,
            hours=12,
            now=later,
            sleep=lambda _s: None,
            limit=3,
        )

    assert report.fetched == 0
    assert report.rejected == 3


def test_not_found_is_raised_rather_than_retried():
    rec = Recorder(httpx.Response(404))
    client = make_client(rec)
    with pytest.raises(ArchiveNotFound):
        client.fetch_named("DE1200_RV2609181435.tar.bz2")
    assert len(rec.requests) == 1


def test_a_name_that_is_not_a_plain_archive_is_refused():
    """The name is built from a datetime, but a URL this code assembles gets checked anyway."""
    client = make_client(Recorder())
    for bad in ("../../etc/passwd", "a/b.tar.bz2", "x.tar.bz2?q=1", "DE1200 RV.tar.bz2"):
        with pytest.raises(ValueError):
            client.fetch_named(bad)


def test_the_pause_does_not_grow_as_the_run_goes_on(db, settings, tmp_path):
    """The jitter is drawn fresh per request and has no memory of earlier ones.

    Worth pinning because a long backfill *can* feel like it is slowing down, and the honest
    cause is retries - not the pause. If someone ever makes this adaptive, the first half of a
    run must still not be systematically quicker than the second.
    """
    rec = Recorder(*[httpx.Response(200, content=_blob()) for _ in range(60)])
    slept: list[float] = []

    with db() as session:
        fetch_missing(
            session,
            make_client(
                rec,
                max_response_bytes=8 * 1024 * 1024,
                hourly_byte_budget=64 * 1024 * 1024,
                daily_byte_budget=512 * 1024 * 1024,
            ),
            LocalArchiveStore(tmp_path / "raw"),
            settings,
            hours=4,
            now=NOMINAL,
            sleep=slept.append,
            limit=41,
        )

    assert len(slept) == 40
    first, second = slept[:20], slept[20:]
    # Not a trend test - just that the tail is not systematically longer than the head.
    assert abs(sum(first) / 20 - sum(second) / 20) < (JITTER_MAX_SECONDS - JITTER_MIN_SECONDS)
    assert max(slept) <= JITTER_MAX_SECONDS


def test_a_rate_limited_request_waits_once(db, settings, tmp_path):
    """Retry-After used to be slept *and* followed by the exponential backoff."""
    from rainalert.radar.client import ServerBusy

    rec = Recorder(
        httpx.Response(429, headers={"Retry-After": "9"}),
        httpx.Response(200, content=_blob()),
    )
    slept: list[float] = []
    client = make_client(
        rec, max_response_bytes=8 * 1024 * 1024, sleep=slept.append, max_attempts=2
    )
    client.fetch_named("DE1200_RV2609161355.tar.bz2")
    assert slept == [9.0]
    assert ServerBusy is not None


def test_backfill_renders_only_the_frame_a_past_cycle_will_ever_show(db, settings, tmp_path):
    """Twenty-four of every twenty-five renders were work thrown away twice.

    The map only ever asks for the *newest* cycle's forecast, and
    overlay_fc_retention_hours deletes a backfilled one within the hour anyway. On a small VM
    that rendering is most of the per-cycle runtime.
    """
    from rainalert.storage import LocalOverlayStore

    overlays = LocalOverlayStore(tmp_path / "ov")
    # Two slots in the window; only the one asking for 13:55 matches the fixture's header.
    rec = Recorder(*[httpx.Response(200, content=_blob()) for _ in range(2)])

    with db() as session:
        report = fetch_missing(
            session,
            make_client(rec, max_response_bytes=8 * 1024 * 1024),
            LocalArchiveStore(tmp_path / "raw"),
            settings,
            hours=5 / 60,
            now=NOMINAL,
            overlays=overlays,
            sleep=lambda _s: None,
            limit=2,
        )

    assert report.fetched == 1
    assert list((tmp_path / "ov" / "obs").glob("*.png")), "the analysis frame must be rendered"
    assert not list((tmp_path / "ov" / "fc").rglob("*.png")), "no forecast frame should be"


def test_live_ingest_still_renders_the_forecast(wet_cycle, tmp_path):
    """The newest cycle's forecast is the whole point of the forward half of the slider."""
    from rainalert.jobs.ingest import render_overlays
    from rainalert.radar.decoder import read_frames
    from rainalert.storage import LocalOverlayStore

    overlays = LocalOverlayStore(tmp_path / "ov")
    frames = read_frames(wet_cycle.read_bytes())

    assert render_overlays(frames, overlays) == len(frames)
    assert list((tmp_path / "ov" / "fc").rglob("*.png"))
