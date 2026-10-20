"""DWD's HDF5 (ODIM_H5) edition of RV, against the binary edition it replaced (DESIGN.md D-74).

The binary edition stopped after 2026-10-20 07:55 UTC. Everything the service is tuned on - the
thresholds, the map bands, the plausibility figures, the verification - was measured on it, so the
new decoder is held to it: the same cycle in both editions must give the same frames. The
fixtures are a real twin pair, 2026-10-18 17:40 UTC, a wet cycle, trimmed to three leads.
"""

import io
import tarfile
from datetime import UTC, datetime

import h5py
import numpy as np
import pytest

from rainalert.radar import odim
from rainalert.radar.decoder import (
    RVArchiveRejected,
    RVFormatError,
    read_analysis_frame,
    read_cycle,
    read_frames,
)
from rainalert.storage import LocalArchiveStore, archive_paths, archive_time

NOMINAL = datetime(2026, 10, 18, 17, 40, tzinfo=UTC)


# --- the twin: same cycle, both editions -------------------------------------------------------


@pytest.fixture(scope="module")
def twins(hdf5_cycle, hdf5_twin):
    return read_cycle(hdf5_cycle), read_cycle(hdf5_twin)


def test_both_editions_describe_the_same_frames(twins):
    new, old = twins
    assert [f.lead_minutes for f in new] == [f.lead_minutes for f in old] == [0, 60, 120]
    for n, o in zip(new, old, strict=True):
        assert n.nominal_time == o.nominal_time == NOMINAL
        assert n.interval_minutes == o.interval_minutes == 5
        assert n.precision == o.precision == 0.01
        assert n.radar_sites == o.radar_sites
        assert n.values.dtype == o.values.dtype == np.float32
        assert n.shape == o.shape == (1200, 1100)


def test_no_data_is_exactly_where_it_was(twins):
    """The orientation test. HDF5 is north-up, the binary edition south-up: decoded without the
    flip, half the country's radar coverage lands in the wrong place, and this mask is the
    sharpest way to see it - it differs in ~170,000 cells unflipped and in none flipped."""
    new, old = twins
    for n, o in zip(new, old, strict=True):
        assert np.array_equal(n.missing, o.missing), f"lead {n.lead_minutes}"
        assert np.array_equal(np.isnan(n.values), o.missing)


def test_values_match_to_the_old_resolution(twins):
    """Binary = HDF5 floored to 0.01 mm, at least 0.01 where rain was detected. The two editions
    were rounded independently from a finer value, so a cell sitting exactly on a 0.01 step can
    land on either side: at most 0.01 mm apart, and only there."""
    new, old = twins
    for n, o in zip(new, old, strict=True):
        valid = ~o.missing
        diff = np.abs(n.values[valid] - o.values[valid])
        assert diff.max() <= 0.01 + 1e-6, f"lead {n.lead_minutes}"
        assert (diff > 1e-6).mean() < 0.15, "more than the step-boundary cells disagree"


def test_every_rain_threshold_sees_the_same_cells(twins):
    """What the warnings actually depend on: rain at or above a subscriber's threshold."""
    new, old = twins
    for n, o in zip(new, old, strict=True):
        valid = ~o.missing
        for threshold in (0.01, 0.05, 0.15, 0.5, 1.0):
            agree = (n.values[valid] >= threshold) == (o.values[valid] >= threshold)
            assert agree.mean() > 0.999, (n.lead_minutes, threshold)


def test_the_analysis_frame_alone(hdf5_cycle, twins):
    new, _ = twins
    frame = read_analysis_frame(hdf5_cycle)
    assert frame.lead_minutes == 0
    assert np.array_equal(frame.values, new[0].values, equal_nan=True)

    lite = read_frames(hdf5_cycle, analysis_only=True)
    assert lite[0].values is not None
    assert all(f.values is None and f.missing is None for f in lite[1:])
    assert [f.lead_minutes for f in lite] == [0, 60, 120]


# --- untrusted input ------------------------------------------------------------------------


def _member(path, name=None):
    with tarfile.open(path, "r:") as tar:
        member = tar.getmembers()[0]
        return (name or member.name), tar.extractfile(member).read()


def _tar(*members: tuple[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, blob in members:
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
    return out.getvalue()


def _edited(blob: bytes, edit) -> bytes:
    """A copy of a real member with one thing changed, written back out as HDF5."""
    out = io.BytesIO()
    with h5py.File(io.BytesIO(blob), "r") as src, h5py.File(out, "w") as dst:
        for key, value in src.attrs.items():
            dst.attrs[key] = value
        for key in src:
            src.copy(src[key], dst, name=key)
        edit(dst)
    return out.getvalue()


def test_a_member_whose_name_disagrees_with_its_attributes_is_refused(hdf5_cycle):
    name, blob = _member(hdf5_cycle)
    with pytest.raises(RVFormatError, match="disagrees"):
        read_frames(_tar((name.replace("_000-", "_060-"), blob)))


def test_garbage_in_an_hdf5_member_is_a_format_error(hdf5_cycle):
    name, _ = _member(hdf5_cycle)
    with pytest.raises(RVFormatError):
        read_frames(_tar((name, b"<html>503 Service Unavailable</html>")))


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda h: h["dataset1/data1/what"].attrs.__setitem__("quantity", b"DBZH"), "quantity"),
        (lambda h: h["dataset1/data1/what"].attrs.__setitem__("gain", 1e20), "gain"),
        (lambda h: h["dataset1/data1/what"].attrs.__setitem__("offset", 5.0), "offset"),
        (lambda h: h["what"].attrs.__setitem__("object", b"PVOL"), "composite"),
        (lambda h: h["dataset1/what"].attrs.__setitem__("starttime", b"173000"), "interval"),
        (lambda h: h["what"].attrs.__setitem__("time", b"150000"), "lead"),
    ],
)
def test_a_member_that_is_not_rv_as_we_know_it_is_refused(hdf5_cycle, edit, message):
    """F-2's rule for the binary header, carried over: validate the ranges, not just the shape."""
    name, blob = _member(hdf5_cycle)
    with pytest.raises(RVFormatError, match=message):
        odim.decode_member(_edited(blob, edit), name)


def test_an_implausible_grid_is_refused_before_it_is_read(hdf5_cycle):
    """The allocation is bounded by the shape check, the HDF5 counterpart of a bz2 bomb."""
    name, blob = _member(hdf5_cycle)

    def huge(h):
        del h["dataset1/data1/data"]
        h["dataset1/data1"].create_dataset("data", shape=(20000, 20000), dtype="u4", chunks=True)

    with pytest.raises(RVFormatError, match="implausible grid"):
        odim.decode_member(_edited(blob, huge), name)


def test_an_oversized_plain_archive_is_refused(monkeypatch, hdf5_cycle):
    from rainalert.radar import decoder

    monkeypatch.setattr(decoder, "MAX_TOTAL_BYTES", 1024)
    with pytest.raises(RVArchiveRejected, match="limit"):
        read_frames(hdf5_cycle.read_bytes())


# --- storage: both editions side by side -----------------------------------------------------


def test_archives_are_named_after_their_edition(tmp_path, hdf5_cycle, hdf5_twin):
    store = LocalArchiveStore(tmp_path)
    assert store.put(NOMINAL, hdf5_cycle.read_bytes()).endswith("/composite_rv_20261018_1740.tar")
    assert store.put(NOMINAL, hdf5_twin.read_bytes()).endswith("/DE1200_RV2610181740.tar.bz2")


def test_both_editions_list_newest_first_and_prune_by_time(tmp_path):
    for name in (
        "DE1200_RV2610200750.tar.bz2",
        "DE1200_RV2610200755.tar.bz2",
        "composite_rv_20261020_0800.tar",
        "composite_rv_20261019_0800.tar",
        "notes.txt",
    ):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in archive_paths(tmp_path)] == [
        "composite_rv_20261020_0800.tar",
        "DE1200_RV2610200755.tar.bz2",
        "DE1200_RV2610200750.tar.bz2",
        "composite_rv_20261019_0800.tar",
    ]
    assert archive_time("composite_rv_20261020_0800.tar") == datetime(
        2026, 10, 20, 8, 0, tzinfo=UTC
    )
    removed = LocalArchiveStore(tmp_path).prune(datetime(2026, 10, 20, 7, 55, tzinfo=UTC))
    assert removed == 2  # 07:50 binary and the day-old HDF5
    assert (tmp_path / "notes.txt").exists()
