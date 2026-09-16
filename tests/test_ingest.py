"""The ingest pipeline, against a real Postgres."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rainalert.config import Settings
from rainalert.db.models import CycleStatus, RadarCycle
from rainalert.jobs.ingest import CycleRejected, ingest_once, prune_archives, validate_cycle
from rainalert.radar.decoder import read_frames
from rainalert.storage import LocalArchiveStore
from tests.test_client import Recorder, make_client

NOW = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)  # just after the fixture's 13:55 cycle


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url="postgresql+psycopg://unused",
        archive_dir=str(tmp_path / "raw"),
        expected_frame_count=3,  # the fixture is a trimmed cycle
        _env_file=None,
    )


@pytest.fixture()
def store(tmp_path):
    return LocalArchiveStore(tmp_path / "raw")


def _client(body: bytes, **kw):
    rec = Recorder(httpx.Response(200, content=body, headers={"ETag": '"v1"'}))
    return make_client(rec, max_response_bytes=8 * 1024 * 1024, **kw), rec


def test_stores_one_cycle(db, settings, store, wet_cycle):
    body = wet_cycle.read_bytes()
    client, rec = _client(body)
    with db() as session:
        outcome = ingest_once(session, client, store, settings, now=NOW)
        assert outcome.status == "ok"
        assert outcome.nominal_time == datetime(2026, 9, 16, 13, 55, tzinfo=UTC)
        row = session.query(RadarCycle).one()
        assert row.status is CycleStatus.OK
        assert row.frame_count == 3
        assert row.bytes == len(body)
        assert row.archive_uri is not None
    assert len(rec.requests) == 1


def test_second_run_sends_a_conditional_request_and_stores_nothing(db, settings, store, wet_cycle):
    body = wet_cycle.read_bytes()
    with db() as session:
        client, _ = _client(body)
        ingest_once(session, client, store, settings, now=NOW)

        rec = Recorder(httpx.Response(304))
        client2 = make_client(rec)
        outcome = ingest_once(session, client2, store, settings, now=NOW)
        assert outcome.status == "not_modified"
        assert rec.requests[0].headers["if-none-match"] == '"v1"'
        assert session.query(RadarCycle).count() == 1


def test_the_same_cycle_twice_yields_one_row(db, settings, store, wet_cycle):
    """At-least-once job execution must not double-store (§4.3 rule 8)."""
    body = wet_cycle.read_bytes()
    with db() as session:
        for _ in range(3):
            client, _ = _client(body)
            ingest_once(session, client, store, settings, now=NOW)
        assert session.query(RadarCycle).count() == 1


def test_a_second_run_cannot_hold_the_lock(db, settings, store, wet_cycle):
    """Two overlapping executions: the second must bow out rather than duplicate the work."""
    body = wet_cycle.read_bytes()
    with db() as first, db() as second:
        from rainalert.db.session import pipeline_lock

        with pipeline_lock(first) as acquired:
            assert acquired
            client, rec = _client(body)
            outcome = ingest_once(second, client, store, settings, now=NOW)
        assert outcome.status == "skipped_locked"
        assert rec.requests == []  # it did not even talk to DWD


def test_rejected_cycle_is_recorded_with_a_reason(db, settings, store, wet_cycle):
    """A gap in the timeline with no explanation is what wastes an afternoon later."""
    body = wet_cycle.read_bytes()
    client, _ = _client(body)
    far_future = NOW + timedelta(days=30)
    with db() as session:
        outcome = ingest_once(session, client, store, settings, now=far_future)
        assert outcome.status == "rejected"
        row = session.query(RadarCycle).one()
        assert row.status is CycleStatus.REJECTED
        assert "in the past" in row.notes
        assert row.archive_uri is None


def test_partial_cycle_is_flagged_not_treated_as_truth(db, settings, store, wet_cycle):
    settings.expected_frame_count = 25  # the fixture has 3
    client, _ = _client(wet_cycle.read_bytes())
    with db() as session:
        outcome = ingest_once(session, client, store, settings, now=NOW)
        assert outcome.status == "partial"
        row = session.query(RadarCycle).one()
        assert row.status is CycleStatus.PARTIAL
        assert "expected 25" in row.notes


def test_halted_ingestion_is_reported_not_swallowed(db, settings, store):
    rec = Recorder(*[httpx.Response(500) for _ in range(10)])
    client = make_client(rec, max_attempts=1, breaker_threshold=1)
    with db() as session:
        ingest_once(session, client, store, settings, now=NOW)
        outcome = ingest_once(session, client, store, settings, now=NOW)
    assert outcome.status == "halted"
    assert "breaker" in outcome.reason


# --- validation ---------------------------------------------------------------------------


def test_future_stamped_cycle_is_rejected(settings, wet_cycle):
    """It would read as the freshest data we ever had and silence the staleness alert."""
    frames = read_frames(wet_cycle)
    with pytest.raises(CycleRejected, match="future"):
        validate_cycle(frames, settings, NOW - timedelta(days=2))


def test_stale_cycle_is_rejected(settings, wet_cycle):
    frames = read_frames(wet_cycle)
    with pytest.raises(CycleRejected, match="in the past"):
        validate_cycle(frames, settings, NOW + timedelta(days=2))


def test_mixed_cycles_are_rejected(settings, outage_cycles):
    frames = read_frames(outage_cycles)
    with pytest.raises(CycleRejected, match="mixes"):
        validate_cycle(frames, settings, datetime(2026, 9, 15, 16, 20, tzinfo=UTC))


def test_implausible_rain_is_rejected(settings, wet_cycle):
    """A poisoned field that reads as rain everywhere must not reach the alerting path."""
    import numpy as np

    frames = read_frames(wet_cycle)
    object.__setattr__(frames[0], "values", np.full_like(frames[0].values, 500.0))
    with pytest.raises(CycleRejected, match="implausible"):
        validate_cycle(frames, settings, NOW)


def test_implausible_coverage_is_rejected(settings, wet_cycle):
    """All-sentinel is the 'warn nobody' shape - it must page, not silently skip everyone."""
    import numpy as np

    frames = read_frames(wet_cycle)
    object.__setattr__(frames[0], "missing", np.ones_like(frames[0].missing))
    with pytest.raises(CycleRejected, match="no-data share"):
        validate_cycle(frames, settings, NOW)


def test_real_cycles_pass_validation(settings, wet_cycle):
    validate_cycle(read_frames(wet_cycle), settings, NOW)


def test_prune_removes_only_old_archives(store, settings):
    store.put(datetime(2026, 9, 14, 0, 0, tzinfo=UTC), b"old")
    store.put(datetime(2026, 9, 16, 13, 55, tzinfo=UTC), b"new")
    assert prune_archives(store, settings, now=NOW) == 1
    assert len(list(store.root.glob("*.tar.bz2"))) == 1
