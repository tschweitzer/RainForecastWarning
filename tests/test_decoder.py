"""Decoder tests, including the golden comparison against wradlib (D-21, DESIGN.md 16.1)."""

from datetime import UTC, datetime

import numpy as np
import pytest

from rainalert.radar.decoder import (
    NODATA,
    RVFormatError,
    decode_frame,
    read_cycle,
    read_frames,
)


def test_header_fields(wet_cycle):
    frames = read_cycle(wet_cycle)
    f = frames[0]
    assert f.nominal_time == datetime(2026, 9, 16, 13, 55, tzinfo=UTC)
    assert f.lead_minutes == 0
    assert f.interval_minutes == 5
    assert f.precision == 0.01
    assert f.values.shape == (1200, 1100)
    assert len(f.radar_sites) == 17
    assert "deboo" in f.radar_sites


def test_frames_are_ordered_by_lead_and_carry_valid_times(wet_cycle):
    frames = read_cycle(wet_cycle)
    assert [f.lead_minutes for f in frames] == [0, 60, 120]
    assert frames[1].valid_time == datetime(2026, 9, 16, 14, 55, tzinfo=UTC)
    assert frames[2].valid_time == datetime(2026, 9, 16, 15, 55, tzinfo=UTC)


def test_nodata_sentinel_is_not_decoded_as_rain(wet_cycle):
    """The bug that would have shipped: 0x29C4 & 0x0FFF == 2500 -> 25.00 mm/5min.

    See docs/DWD_RV_FORMAT.md section 8.
    """
    assert NODATA & 0x0FFF == 2500  # the trap is real
    f = read_cycle(wet_cycle)[0]
    assert f.missing.mean() == pytest.approx(0.4673, abs=1e-4)
    assert np.isnan(f.values[f.missing]).all()
    # nothing anywhere near the phantom 25.00 mm reading
    assert np.nanmax(f.values) < 20.0


def test_missing_region_moves_with_lead(wet_cycle):
    """It does not merely grow - it advects (DWD_RV_FORMAT.md section 9)."""
    f0, _, f120 = read_cycle(wet_cycle)
    gained = int((~f0.missing & f120.missing).sum())
    lost = int((f0.missing & ~f120.missing).sum())
    assert gained == 102615
    assert lost == 79483


def test_rejects_truncated_payload(wet_cycle):
    import tarfile

    with tarfile.open(wet_cycle) as tar:
        blob = tar.extractfile(tar.getmembers()[0]).read()
    with pytest.raises(RVFormatError, match="payload"):
        decode_frame(blob[:-2])


def test_rejects_non_radolan_input():
    with pytest.raises(RVFormatError, match="ETX"):
        decode_frame(b"not a radolan file at all")


@pytest.mark.golden
def test_matches_wradlib_exactly(wet_cycle, tmp_path):
    """Bit-identical to the reference implementation: values and no-data mask."""
    wradlib = pytest.importorskip("wradlib")
    import tarfile

    with tarfile.open(wet_cycle) as tar:
        member = tar.getmembers()[0]
        tar.extract(member, tmp_path, filter="data")
        path = tmp_path / member.name
        mine = decode_frame(path.read_bytes())

    data, meta = wradlib.io.read_radolan_composite(str(path))

    assert meta["precision"] == mine.precision
    assert meta["intervalseconds"] == mine.interval_minutes * 60
    assert (meta["nrow"], meta["ncol"]) == mine.values.shape
    assert meta["predictiontime"] == mine.lead_minutes

    # wradlib reports no-data as -9999.0, not NaN, and exposes the mask as flat indices.
    wr_missing = np.zeros(data.size, dtype=bool)
    wr_missing[meta["nodatamask"]] = True
    wr_missing = wr_missing.reshape(data.shape)

    assert np.array_equal(wr_missing, mine.missing)
    valid = ~mine.missing
    assert np.array_equal(mine.values[valid], data[valid].astype(np.float32))


def test_analysis_only_keeps_every_header_and_one_grid(wet_cycle):
    """Backfill validates against all the headers and renders one frame.

    Building the other grids is work and memory spent on values nothing reads.
    """
    full = read_frames(wet_cycle)
    lean = read_frames(wet_cycle, analysis_only=True)

    assert [f.lead_minutes for f in lean] == [f.lead_minutes for f in full]
    assert [f.nominal_time for f in lean] == [f.nominal_time for f in full]
    assert [f.radar_sites for f in lean] == [f.radar_sites for f in full]

    analysis = [f for f in lean if f.lead_minutes == 0]
    assert len(analysis) == 1
    assert analysis[0].values is not None
    # and it is the same data a full read produces
    reference = next(f for f in full if f.lead_minutes == 0)
    assert np.array_equal(analysis[0].values, reference.values, equal_nan=True)
    assert np.array_equal(analysis[0].missing, reference.missing)

    # every other frame carries None rather than an empty array, so a caller that forgets the
    # distinction raises instead of averaging over nothing
    for frame in lean:
        if frame.lead_minutes != 0:
            assert frame.values is None and frame.missing is None


def test_analysis_only_still_refuses_a_truncated_member(wet_cycle):
    """Skipping the grid must not skip the check that the grid is there."""
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as out:
        with tarfile.open(wet_cycle) as src:
            member = next(m for m in src.getmembers() if m.isfile())
            blob = src.extractfile(member).read()
        cut = blob[: len(blob) // 2]
        info = tarfile.TarInfo(member.name)
        info.size = len(cut)
        out.addfile(info, io.BytesIO(cut))

    with pytest.raises(RVFormatError):
        read_frames(buffer.getvalue(), analysis_only=True)
