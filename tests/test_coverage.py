"""Which radar is down, and saying so where a subscriber looks (DESIGN.md D-75)."""

import io
import tarfile
from datetime import UTC, datetime, timedelta

import h5py
import pytest

from rainalert.config import Settings
from rainalert.coverage import coverage_note
from rainalert.db.models import CycleStatus, Evaluation, RadarCycle, Subscription
from rainalert.radar import odim
from rainalert.radar.decoder import RVFormatError, read_frames
from rainalert.radar.sites import SITES, responsible

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
HANNOVER = (52.3759, 9.7320)
MUNICH = (48.1351, 11.5820)


@pytest.fixture()
def settings():
    return Settings(
        database_url="postgresql+psycopg://unused",
        public_base_url="https://rain.example.org",
        secret_key="test-secret",
        _env_file=None,
    )


# --- the radars ----------------------------------------------------------------------------


def test_all_seventeen_radars_are_known():
    """The 16 in a normal cycle's `how/nodes` plus Hannover, missing from the 07:55 one."""
    assert len(SITES) == 17
    assert all(code.startswith("de") and len(code) == 5 for code in SITES)


def test_the_missing_radar_that_covers_the_point_is_named():
    assert responsible(*HANNOVER, ["dehnr"]).name == "Hannover"
    # Two down: the nearer one that reaches the point.
    assert responsible(*HANNOVER, ["deboo", "dehnr"]).name == "Hannover"


def test_a_missing_radar_out_of_range_is_not_blamed():
    """Hannover is ~500 km from Munich: whatever is missing there, it is not that."""
    assert responsible(*MUNICH, ["dehnr"]) is None
    assert responsible(*MUNICH, []) is None
    assert responsible(*MUNICH, ["xxnope"]) is None


# --- the decoder reads them, the ingest job stores them -------------------------------------


def _with_missing(path, value: bytes) -> bytes:
    """The fixture archive with `how/missing_nodes` set in every member."""
    out = io.BytesIO()
    with tarfile.open(path, "r:") as src, tarfile.open(fileobj=out, mode="w") as dst:
        for member in src.getmembers():
            edited = io.BytesIO(src.extractfile(member).read())
            with h5py.File(edited, "r+") as h5:
                h5["how"].attrs["missing_nodes"] = value
            blob = edited.getvalue()
            info = tarfile.TarInfo(member.name)
            info.size = len(blob)
            dst.addfile(info, io.BytesIO(blob))
    return out.getvalue()


def test_the_decoder_reads_the_missing_radars(hdf5_cycle):
    frames = read_frames(_with_missing(hdf5_cycle, b"'dehnr','deess'"))
    assert {f.missing_sites for f in frames} == {("dehnr", "deess")}
    assert read_frames(hdf5_cycle)[0].missing_sites == ()


def test_the_ingest_job_stores_them_with_the_cycle(db, tmp_path, hdf5_cycle):
    import httpx

    from rainalert.jobs.ingest import ingest_once
    from rainalert.storage import LocalArchiveStore
    from tests.helpers import Recorder, make_client

    body = _with_missing(hdf5_cycle, b"'dehnr'")
    client = make_client(
        Recorder(httpx.Response(200, content=body, headers={"ETag": '"v1"'})),
        max_response_bytes=8 * 1024 * 1024,
    )
    settings = Settings(
        database_url="postgresql+psycopg://unused", expected_frame_count=3, _env_file=None
    )
    with db() as session:
        outcome = ingest_once(
            session,
            client,
            LocalArchiveStore(tmp_path),
            settings,
            now=datetime(2026, 10, 18, 17, 44, tzinfo=UTC),
        )
        assert outcome.status == "ok"
        assert session.query(RadarCycle).one().missing_sites == ["dehnr"]


# --- the grid check (D-75) -------------------------------------------------------------------


def _first_member(path):
    with tarfile.open(path, "r:") as tar:
        member = tar.getmembers()[0]
        return member.name, tar.extractfile(member).read()


def _edit_where(blob: bytes, **attrs) -> bytes:
    edited = io.BytesIO(blob)
    with h5py.File(edited, "r+") as h5:
        for key, value in attrs.items():
            h5["where"].attrs[key] = value
    return edited.getvalue()


@pytest.mark.parametrize(
    ("attrs", "why"),
    [
        # The sphere this service wrongly assumed until D-75: exactly the mismatch to catch.
        (
            {
                "projdef": b"+proj=stere +lat_ts=60 +lat_0=90 +lon_0=10 +x_0=542962.166921856585 "
                b"+y_0=3609144.7242655745 +units=m +a=6370040 +b=6370040 +no_defs"
            },
            "projection a=",
        ),
        ({"LL_lon": 3.60}, "LL corner"),
        ({"xscale": 2000.0}, "cells are"),
        ({"xsize": 1000}, "where/ says"),
    ],
)
def test_a_cycle_on_a_different_grid_is_refused(hdf5_cycle, attrs, why):
    name, blob = _first_member(hdf5_cycle)
    with pytest.raises(RVFormatError, match=f"radar grid changed: .*{why}"):
        odim.decode_member(_edit_where(blob, **attrs), name)


def test_the_real_grid_passes(hdf5_cycle):
    name, blob = _first_member(hdf5_cycle)
    assert odim.decode_member(blob, name, header_only=True).lead_minutes == 0


# --- the note on the settings page ---------------------------------------------------------


def _subscription(session, settings, at=HANNOVER):
    from rainalert import subscriptions as svc

    result = svc.subscribe(session, settings, address="friend@example.com", lat=at[0], lon=at[1])
    confirmed = svc.confirm(session, settings, token=result.confirm_token)
    return session.query(Subscription).filter_by(subscriber_id=confirmed.subscriber_id).one()


def _evaluated(session, sub, decision, missing, at=NOW):
    cycle = RadarCycle(
        nominal_time=at,
        fetched_at=at,
        source_url="test",
        sha256=b"\0" * 32,
        bytes=1,
        frame_count=25,
        status=CycleStatus.OK,
        missing_sites=missing,
    )
    session.add(cycle)
    session.flush()
    session.add(
        Evaluation(
            subscription_id=sub.id,
            cycle_id=cycle.id,
            evaluated_at=at,
            now_wet=False,
            first_hit_lead_minutes=None,
            max_rate_by_lead=[None] * 25,
            missing_fraction=[1.0] * 25,
            state_before="dry",
            state_after="dry",
            decision=decision,
        )
    )
    session.commit()


def test_no_data_here_names_the_radar_that_is_down(db, settings):
    with db() as session:
        sub = _subscription(session, settings)
        _evaluated(session, sub, "skipped_missing", ["dehnr"])
        note = coverage_note(session, sub, now=NOW + timedelta(minutes=3))
    assert "Radar Hannover" in note
    assert "nicht warnen" in note


def test_no_data_without_a_radar_to_blame_says_only_that(db, settings):
    with db() as session:
        sub = _subscription(session, settings, at=MUNICH)
        _evaluated(session, sub, "skipped_missing", ["dehnr"])
        note = coverage_note(session, sub, now=NOW)
    assert note.startswith("An deinem Ort fehlen gerade Radardaten.")
    assert "Hannover" not in note


def test_no_note_when_there_was_data_or_it_is_old_news(db, settings):
    with db() as session:
        sub = _subscription(session, settings)
        _evaluated(session, sub, "no_rain", ["dehnr"])
        assert coverage_note(session, sub, now=NOW) is None

        later = NOW + timedelta(minutes=5)
        _evaluated(session, sub, "skipped_missing", ["dehnr"], at=later)
        assert coverage_note(session, sub, now=later + timedelta(minutes=20)) is None, (
            "an hour-old gap is the stale banner's job, not this"
        )


def test_the_settings_endpoint_carries_it(db, settings):
    from fastapi.testclient import TestClient

    from rainalert.api.app import create_app
    from rainalert.notify import ConsoleNotifier

    notifier = ConsoleNotifier()
    client = TestClient(create_app(settings, session_factory=db, notifier=notifier))
    client.post(
        "/api/v1/subscriptions",
        json={"email": "friend@example.com", "lat": HANNOVER[0], "lon": HANNOVER[1]},
    )
    token = notifier.sent[-1].text.split("/confirm#")[1].split("=", 1)[1].split()[0]
    page = client.post("/confirm", data={"token": token})
    api_token = page.text.split("<code>")[1].split("</code>")[0].strip()
    with db() as session:
        sub = session.query(Subscription).one()
        _evaluated(session, sub, "skipped_missing", ["dehnr"], at=datetime.now(UTC))
    me = client.get("/api/v1/subscriptions/me", headers={"Authorization": f"Bearer {api_token}"})
    assert "Radar Hannover" in me.json()["coverage_note"]


def test_the_settings_page_shows_it():
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "rainalert" / "api" / "static" / "settings.js"
    ).read_text(encoding="utf-8")
    assert "notices.push(current.coverage_note)" in script
