"""Regression test for the frame-0-only data-quality gate.

A real two-cycle dropout of the Borkum radar on 2026-09-15. Its value is that at 16:15 the
*analysis* frame is clean while the forecast frames are already gone - so a gate that checks only
frame 0 passes the cycle and then reads empty frames as "no rain", concluding dry for a location it
has no data for. See docs/DWD_RV_FORMAT.md section 5.
"""

import logging
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from rainalert.radar.decoder import read_frames
from rainalert.radar.grid import radius_mask

BORKUM = (53.58, 6.66)  # inside the dropout
HAMBURG = (53.55, 9.99)  # control, unaffected throughout
RADIUS_M = 2000


def _by_cycle(path):
    frames = read_frames(path)
    out: dict[str, list] = {}
    for f in frames:
        out.setdefault(f.nominal_time.strftime("%H%M"), []).append(f)
    return {k: sorted(v, key=lambda f: f.lead_minutes) for k, v in out.items()}


def _missing_fraction(frame, lat, lon):
    rows, cols = radius_mask(lat, lon, RADIUS_M)
    return float(frame.missing[rows, cols].mean())


@pytest.fixture(scope="module")
def cycles(outage_cycles):
    return _by_cycle(outage_cycles)


def test_fixture_covers_the_dropout_and_its_recovery(cycles):
    assert sorted(cycles) == ["1615", "1620", "1625", "1630"]
    assert all(len(v) == 3 for v in cycles.values())


def test_analysis_is_clean_while_the_forecast_is_already_gone(cycles):
    """16:15 - the case that defeats a frame-0-only gate."""
    frames = cycles["1615"]
    assert _missing_fraction(frames[0], *BORKUM) == 0.0
    for frame in frames[1:]:
        assert _missing_fraction(frame, *BORKUM) == 1.0


def test_dropout_cycles_are_missing_at_every_lead(cycles):
    for stamp in ("1620", "1625"):
        for frame in cycles[stamp]:
            assert _missing_fraction(frame, *BORKUM) == 1.0, stamp


def test_coverage_returns(cycles):
    for frame in cycles["1630"]:
        assert _missing_fraction(frame, *BORKUM) == 0.0


def test_control_location_is_unaffected_throughout(cycles):
    """A gate that blanket-suppresses everything must fail this."""
    for stamp, frames in cycles.items():
        for frame in frames:
            assert _missing_fraction(frame, *HAMBURG) == 0.0, stamp


def test_ms_site_list_is_not_a_coverage_signal(cycles):
    """deasb and deboo are absent from MS at 16:30, yet coverage is fully restored."""
    assert "deasb" in cycles["1615"][0].radar_sites
    for stamp in ("1620", "1625", "1630"):
        assert "deasb" not in cycles[stamp][0].radar_sites
    assert _missing_fraction(cycles["1630"][0], *BORKUM) == 0.0


def test_national_extent_of_the_dropout(cycles):
    """~5.8 % of the grid goes dark, centred on the North Sea."""
    m15 = cycles["1615"][0].missing
    m20 = cycles["1620"][0].missing
    extra = m20 & ~m15
    assert int(extra.sum()) == 76514
    assert np.isclose(extra.mean(), 0.058, atol=0.001)


@pytest.fixture()
def settings():
    """Defaults are what matters here: `timeline_stale_after_minutes` is read, not overridden, so
    these tests move with the threshold rather than pinning a number of their own."""
    from rainalert.config import Settings

    return Settings(database_url="postgresql+psycopg://unused", _env_file=None)


# --- the staleness log the free alert matches ---------------------------------------------------
#
# These assert on log *text*, which is normally a smell - but here the text is the interface. The
# alert in infra/monitoring.tf is a log-based metric filtering these exact phrases, chosen because
# the SLI lives in the database where Cloud Monitoring cannot reach it and the alternatives
# (Prometheus, a scheduled job writing a custom metric) each add a billable resource. So a reworded
# log line is a silently disabled alert, and `test_the_alert_filter_matches_the_words_logged` below
# ties the two together.


def _cycle_at(session, when):
    from rainalert.db.models import CycleStatus, RadarCycle

    session.add(
        RadarCycle(
            nominal_time=when,
            fetched_at=when,
            source_url="test",
            sha256=b"\x00" * 32,
            bytes=1,
            frame_count=3,
            status=CycleStatus.OK,
        )
    )
    session.commit()


def test_fresh_radar_logs_nothing(db, settings, caplog):
    from rainalert.jobs.ingest import log_cycle_staleness

    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    with db() as session:
        _cycle_at(session, now - timedelta(minutes=5))
        with caplog.at_level(logging.ERROR):
            assert log_cycle_staleness(session, settings, now) is None
    assert not caplog.records


def test_no_cycles_at_all_logs_nothing(db, settings, caplog):
    """Deliberately silent. A fresh deployment is empty between `migrate` and the first ingest run,
    and paging then would teach an operator to ignore this alert on the one day they are certainly
    watching. `job_not_completing` covers a first run that never happens."""
    from rainalert.jobs.ingest import log_cycle_staleness

    with db() as session, caplog.at_level(logging.ERROR):
        assert log_cycle_staleness(session, settings) is None
    assert not caplog.records


def test_stale_radar_is_logged_with_the_words_the_alert_filters(db, settings, caplog):
    from rainalert.jobs.ingest import log_cycle_staleness

    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    with db() as session:
        _cycle_at(session, now - timedelta(minutes=45))
        with caplog.at_level(logging.ERROR):
            assert log_cycle_staleness(session, settings, now) == "stale"
    assert "radar data is stale" in caplog.text
    assert "45 min old" in caplog.text


def test_the_threshold_is_the_one_the_page_shows_a_banner_for(db, settings, caplog):
    """An operator being paged and a reader looking at the map must not disagree about whether the
    radar is stale, so both read `timeline_stale_after_minutes`."""
    from rainalert.jobs.ingest import log_cycle_staleness

    limit = settings.timeline_stale_after_minutes
    stamped = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

    # One cycle, two clocks. The first version of this inserted a second cycle instead, which does
    # not test the boundary at all: both rows live in one database and the function reads the
    # *newest*, so the "just inside" row stayed newest and the second check never saw a stale value.
    with db() as session:
        _cycle_at(session, stamped)
        with caplog.at_level(logging.ERROR):
            just_inside = stamped + timedelta(minutes=limit - 1)
            assert log_cycle_staleness(session, settings, just_inside) is None, (
                "one minute inside the threshold is not stale"
            )
            just_outside = stamped + timedelta(minutes=limit + 1)
            assert log_cycle_staleness(session, settings, just_outside) == "stale", (
                "one minute outside it is"
            )


def test_a_cycle_stamped_in_the_future_is_its_own_condition(db, settings, caplog):
    """A future timestamp reads as "the freshest data we ever had" and would silence a threshold on
    age until real time caught up (SECURITY_REVIEW.md F-7). The sign is the signal, not the size, so
    it gets its own sentence rather than a bigger number."""
    from rainalert.jobs.ingest import log_cycle_staleness

    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    with db() as session:
        _cycle_at(session, now + timedelta(hours=3))
        with caplog.at_level(logging.ERROR):
            assert log_cycle_staleness(session, settings, now) == "future"
    assert "cycle timestamp is in the future" in caplog.text
    # And it must not also claim staleness: the age is negative, not large.
    assert "radar data is stale" not in caplog.text


def test_the_alert_filter_matches_the_words_logged(db, settings, caplog):
    """The log line is the alert's interface, so the two are asserted against each other.

    `infra/monitoring.tf` filters these phrases out of the ingest job's logs. Rewording either side
    alone leaves a metric that never increments and an alert that never fires - with nothing failing
    anywhere, which is the exact shape of silent breakage this whole alert exists to remove.
    """
    import pathlib

    from rainalert.jobs.ingest import log_cycle_staleness

    monitoring = (
        pathlib.Path(__file__).resolve().parents[1] / "infra" / "monitoring.tf"
    ).read_text(encoding="utf-8")
    block = monitoring[monitoring.index('resource "google_logging_metric" "stale_radar"') :]
    block = block[: block.index("\nresource ")]

    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    logged = []
    for offset, expected in ((timedelta(minutes=45), "stale"), (-timedelta(hours=3), "future")):
        with db() as session:
            _cycle_at(session, now - offset)
            caplog.clear()
            with caplog.at_level(logging.ERROR):
                assert log_cycle_staleness(session, settings, now) == expected
            logged.append(caplog.text)

    for phrase in ("radar data is stale", "cycle timestamp is in the future"):
        assert phrase in block, f"monitoring.tf no longer filters {phrase!r}"
        assert any(phrase in text for text in logged), f"nothing logs {phrase!r} any more"
