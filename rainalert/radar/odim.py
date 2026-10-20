"""Decoder for DWD's HDF5 (ODIM_H5) edition of the RV composite (DESIGN.md D-74).

DWD stopped publishing the binary ``DE1200_RV*.tar.bz2`` edition after the 2026-10-20 07:55 UTC
cycle; ``composite_rv_<YYYYMMDD>_<HHMM>.tar`` carries the same product as an uncompressed tar of
25 HDF5 files, ``composite_rv_<YYYYMMDD>_<HHMM>_<LLL>-hd5``, one per forecast lead. The layout is
in ``docs/DWD_RV_FORMAT.md`` §11. In short, for each member:

- ``dataset1/data1/data``: uint32, (1200, 1100), gzip-compressed inside the file, row 0 at the
  **northern** edge (ODIM's upper-left origin) - the binary edition has row 0 at the south.
- ``dataset1/data1/what``: ``value = raw * gain + offset`` with gain 0.001 and offset -0.001, so
  raw 1 is a detected 0.000 mm; ``undetect`` (0) is no rain, ``nodata`` (2^32 - 1) no measurement.
- ``what/date`` + ``what/time``: the cycle's nominal time. ``dataset1/what/end*``: the time the
  frame is valid for, with ``start*`` five minutes earlier.

The output is the same ``RVFrame`` the binary decoder produces, and on purpose the same *values*:
measured on two twin cycles (2026-10-18 17:40, a wet one, and 2026-10-20 07:55), the binary edition
is this one floored to 0.01 mm, with every detected cell at least 0.01. Reproducing that keeps
every threshold, map band and plausibility figure the service was tuned on exactly where it was;
the only cells that differ are the ones sitting exactly on a 0.01 step, by at most 0.01 mm,
because the two editions were rounded independently from a finer value. The finer resolution is
there to be used later - deliberately not in the change made during an outage.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import UTC, datetime, timedelta

import numpy as np

from rainalert.radar.decoder import (
    EXPECTED_SHAPE,
    MAX_DIM,
    MAX_LEAD_MINUTES,
    MIN_DIM,
    VALID_INTERVALS,
    RVFormatError,
    RVFrame,
)
from rainalert.radar.grid import DE1200, outer_corners

logger = logging.getLogger(__name__)

#: The resolution the values are reported at - the binary edition's, see the module docstring.
PRECISION = 0.01
#: Accepted gains. DWD's is 0.001, stored as the float32 nearest to it.
VALID_GAINS = (1e-3,)
#: How far a stated grid corner may be from ours before the cycle is refused (D-75). They agree to
#: well under a metre; a real change of grid moves them by hundreds of metres or more.
CORNER_TOLERANCE_M = 50.0
#: The projection DWD states, as parameters. `x_0`/`y_0` are left out: they only move the origin,
#: which the corner check covers.
EXPECTED_PROJECTION = {
    "proj": "stere",
    "lat_0": 90.0,
    "lat_ts": 60.0,
    "lon_0": 10.0,
    "a": 6378137.0,
    "b": 6356752.3142451802,
    "units": "m",
}
#: `composite_rv_20261020_0755_060-hd5` - the lead is cross-checked against the attributes.
MEMBER_NAME = re.compile(r"composite_rv_(\d{8})_(\d{4})_(\d{3})-hd5$")


def is_member_name(name: str) -> bool:
    return MEMBER_NAME.search(name) is not None


def decode_member(blob: bytes, name: str = "", *, header_only: bool = False) -> RVFrame:
    """Decode one HDF5 member into an ``RVFrame``. ``header_only`` as in ``decode_frame``."""
    import h5py  # lazy: the binary path and the web tier never need it

    try:
        with h5py.File(io.BytesIO(blob), "r") as h5:
            fields = _fields(h5)
            if name:
                _check_name(name, fields)
            if header_only:
                return _frame(None, None, fields)
            raw = _data(h5, fields)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        if isinstance(exc, RVFormatError):
            raise
        raise RVFormatError(f"not a readable ODIM HDF5 member: {exc}") from exc

    # North-up as delivered; south-up as the rest of the service has always read it.
    raw = raw[::-1]
    missing = raw == fields["nodata"]
    detected = (raw != fields["undetect"]) & ~missing
    mm = raw.astype(np.float64) * fields["gain"] + fields["offset"]
    # Rounded to 6 places before the floor, so a value that is a whole step apart from float noise
    # (21 * 0.0009999999 - 0.0009999999 = 0.0199999986) lands on that step.
    steps = np.maximum(1.0, np.floor(np.round(mm * 100.0, 6)))
    values = np.where(detected, steps * PRECISION, 0.0)
    values = np.where(missing, np.nan, values).astype(np.float32)
    return _frame(values, missing, fields)


def _attr(group, key: str) -> str:
    value = group.attrs[key]
    if isinstance(value, bytes):
        value = value.decode("ascii")
    if isinstance(value, np.ndarray):
        value = value.item()
    return str(value)


def _number(group, key: str) -> float:
    value = group.attrs[key]
    if isinstance(value, np.ndarray):
        value = value.item()
    return float(value)


def _stamp(date: str, time: str) -> datetime:
    try:
        return datetime.strptime(date + time, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise RVFormatError(f"malformed timestamp {date!r} {time!r}") from exc


def _fields(h5) -> dict:
    conventions = h5.attrs.get("Conventions", b"")
    if isinstance(conventions, bytes):
        conventions = conventions.decode("ascii", "replace")
    if not str(conventions).startswith("ODIM_H5"):
        raise RVFormatError(f"not an ODIM_H5 file (Conventions {conventions!r})")
    if _attr(h5["what"], "object") != "COMP":
        raise RVFormatError("not a composite")
    data_what = h5["dataset1/data1/what"]
    if _attr(data_what, "quantity") != "ACRR":
        raise RVFormatError(f"unexpected quantity {_attr(data_what, 'quantity')!r}")

    nominal = _stamp(_attr(h5["what"], "date"), _attr(h5["what"], "time"))
    what = h5["dataset1/what"]
    start = _stamp(_attr(what, "startdate"), _attr(what, "starttime"))
    end = _stamp(_attr(what, "enddate"), _attr(what, "endtime"))
    interval = int((end - start) / timedelta(minutes=1))
    lead = int((end - nominal) / timedelta(minutes=1))
    if interval not in VALID_INTERVALS:
        raise RVFormatError(f"unexpected interval {interval} min")
    if not 0 <= lead <= MAX_LEAD_MINUTES or lead % 5:
        raise RVFormatError(f"implausible forecast lead {lead} min")

    gain, offset = _number(data_what, "gain"), _number(data_what, "offset")
    if not any(abs(gain - g) <= g * 1e-4 for g in VALID_GAINS):
        raise RVFormatError(f"unexpected gain {gain!r}")
    if not -2 * gain <= offset <= 0:
        raise RVFormatError(f"unexpected offset {offset!r}")

    dataset = h5["dataset1/data1/data"]
    rows, cols = dataset.shape if len(dataset.shape) == 2 else (0, 0)
    if not MIN_DIM <= rows <= MAX_DIM or not MIN_DIM <= cols <= MAX_DIM:
        raise RVFormatError(f"implausible grid {dataset.shape}")
    if dataset.dtype != np.uint32:
        raise RVFormatError(f"unexpected data type {dataset.dtype}")
    if (rows, cols) != EXPECTED_SHAPE:
        logger.warning("RV grid is %dx%d, expected %dx%d", rows, cols, *EXPECTED_SHAPE)

    _check_grid(h5["where"], rows, cols)
    sites = _sites(h5["how"], "nodes")
    missing_sites = _sites(h5["how"], "missing_nodes")
    software = _attr(h5["how"], "sw_version") if "sw_version" in h5["how"].attrs else ""
    return {
        "nominal_time": nominal,
        "lead_minutes": lead,
        "interval_minutes": interval,
        "rows": rows,
        "cols": cols,
        "gain": gain,
        "offset": offset,
        "nodata": int(_number(data_what, "nodata")),
        "undetect": int(_number(data_what, "undetect")),
        "radar_sites": sites,
        "missing_sites": missing_sites,
        # Stands in for the binary header: what a log line or `rainalert probe` needs to identify
        # the file, not a format anyone parses.
        "raw_header": (
            f"{conventions} RV {nominal:%Y%m%d%H%M} lead {lead:03d} {software} "
            f"nodes <{','.join(sites)}>"
        ),
    }


def _sites(how, key: str) -> tuple[str, ...]:
    """`'deasb','deboo'` -> ("deasb", "deboo"); absent or empty -> ()."""
    if key not in how.attrs:
        return ()
    return tuple(s.strip().strip("'") for s in _attr(how, key).split(",") if s.strip().strip("'"))


def _check_grid(where, rows: int, cols: int) -> None:
    """Refuse a cycle whose stated grid is not the one `radar/grid.py` assumes (D-75).

    Every location, radius and map pixel in the service is computed from that grid, and a change on
    DWD's side would not fail anything: the data would decode fine and every warning would be for
    a place up to some kilometres away. So the grid DWD states in the file is compared with ours on
    every member, and a difference stops the cycle with its own log line - an alert, not a gap.
    """
    from pyproj import Geod

    problems = []
    if (rows, cols) != (DE1200.rows, DE1200.cols):
        problems.append(f"{rows}x{cols} cells, expected {DE1200.rows}x{DE1200.cols}")
    try:
        size = (int(_number(where, "ysize")), int(_number(where, "xsize")))
        scale = (_number(where, "yscale"), _number(where, "xscale"))
    except KeyError as exc:
        raise RVFormatError(f"radar grid changed: no {exc} in where/") from exc
    if size != (rows, cols):
        problems.append(f"where/ says {size[0]}x{size[1]} cells, the data is {rows}x{cols}")
    if scale != (DE1200.res_km * 1000, DE1200.res_km * 1000):
        problems.append(f"cells are {scale[1]:g} x {scale[0]:g} m, expected 1000 x 1000")
    params = {}
    for token in _attr(where, "projdef").split():
        key, _, value = token.lstrip("+").partition("=")
        params[key] = value
    for key, expected in EXPECTED_PROJECTION.items():
        value = params.get(key)
        if isinstance(expected, float):
            try:
                same = value is not None and abs(float(value) - expected) <= 1e-6 * max(1, expected)
            except ValueError:
                same = False
        else:
            same = value == expected
        if not same:
            problems.append(f"projection {key}={value!r}, expected {expected!r}")
    geod = Geod(ellps="WGS84")
    for corner, (lat, lon) in outer_corners().items():
        stated = (_number(where, f"{corner}_lat"), _number(where, f"{corner}_lon"))
        _, _, distance = geod.inv(lon, lat, stated[1], stated[0])
        if distance > CORNER_TOLERANCE_M:
            problems.append(f"{corner} corner is {distance:.0f} m from ours")
    if problems:
        raise RVFormatError("radar grid changed: " + "; ".join(problems))


def _check_name(name: str, fields: dict) -> None:
    match = MEMBER_NAME.search(name)
    if match is None:
        raise RVFormatError(f"unexpected member name {name!r}")
    stamp = _stamp(match.group(1), match.group(2) + "00")
    if stamp != fields["nominal_time"] or int(match.group(3)) != fields["lead_minutes"]:
        raise RVFormatError(f"member {name!r} disagrees with its own attributes")


def _data(h5, fields: dict) -> np.ndarray:
    # The shape was checked in `_fields`, so this allocation is bounded before it happens - the
    # HDF5 counterpart of refusing a bz2 member that declares too much.
    return h5["dataset1/data1/data"][()]


def _frame(values, missing, fields: dict) -> RVFrame:
    return RVFrame(
        values=values,
        missing=missing,
        nominal_time=fields["nominal_time"],
        lead_minutes=fields["lead_minutes"],
        interval_minutes=fields["interval_minutes"],
        precision=PRECISION,
        radar_sites=fields["radar_sites"],
        raw_header=fields["raw_header"],
        missing_sites=fields["missing_sites"],
    )
