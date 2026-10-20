"""Georeferencing for the DE1200 RADOLAN grid.

DE1200 is a polar-stereographic grid on the WGS84 ellipsoid, 1200 rows x 1100 columns of nominally
1 km cells (see `RADOLAN_PROJ` for why not the sphere this said until D-75). Its definition is
pinned here as constants and verified against ``wradlib.georef.get_radolan_grid`` in the tests -
wradlib is the oracle, not a runtime dependency (D-21) - and, since D-75, against the corners DWD
states in every HDF5 file, on every cycle.

Two conventions that are easy to get wrong, and are the reason the tests compare the whole grid
rather than a few points:

* **Row 0 is the southern edge.** Images must be flipped vertically to render north-up.
* The projected coordinates of a cell are its **lower-left corner**, not its centre. Anything
  distance-related here uses centres (corner + half a cell).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from pyproj import CRS, Geod, Transformer

#: The projection DE1200 is defined on: polar stereographic on the **WGS84 ellipsoid** (D-75).
#:
#: It was the RADOLAN sphere (R = 6370040 m) until 2026-10-20, and that was wrong for this grid.
#: The sphere is the definition of DWD's older national grids; DE1200 - RADOLAN format version 5,
#: which every binary header we decoded said (`VS 5`) - is on the ellipsoid ("Radarkomposits -
#: Projektionen und Gitter" v1.01, 2022; wradlib switches the same way for VS >= 5). The HDF5
#: edition now states it in every file, with the grid's corner coordinates, and those corners are
#: reproduced to well under a metre by this string with the reference point below. With the
#: sphere, a cell's centre was 80 m (Frankfurt) to 1.3 km (Görlitz) from where DWD puts it.
RADOLAN_PROJ = (
    "+proj=stere +lat_0=90 +lat_ts=60 +lon_0=10 +x_0=0 +y_0=0 "
    "+a=6378137 +b=6356752.3142451802 +units=km +no_defs"
)

#: Reference point DWD ties the grid to.
REF_LON, REF_LAT = 9.0, 51.0


@dataclass(frozen=True)
class GridSpec:
    rows: int
    cols: int
    res_km: float
    j_0: float  # column offset of the reference point
    i_0: float  # row offset of the reference point


DE1200 = GridSpec(rows=1200, cols=1100, res_km=1.0, j_0=470.0, i_0=600.0)

_GEOD = Geod(ellps="WGS84")


class OutsideGrid(LookupError):
    """The coordinate cannot be mapped onto the DE1200 grid.

    Raised for anything this module refuses: outside the rectangle, or not a finite number. It is
    the *only* exception ``cell_of`` and ``radius_mask`` raise for a bad coordinate - a documented
    exception type that does not actually cover the failure modes is worse than none, because a
    caller that carefully catches it still dies on the cases it missed.

    Note this is *not* the same as being outside radar coverage: roughly 47 % of the grid is
    permanently no-data because the rectangle is much larger than the radar network's reach.
    Coverage is a property of the data (see ``RVFrame.missing``), not of the geometry.
    """


@lru_cache(maxsize=2)
def _to_radolan() -> Transformer:
    return Transformer.from_crs(CRS.from_epsg(4326), CRS.from_proj4(RADOLAN_PROJ), always_xy=True)


@lru_cache(maxsize=2)
def _to_wgs84() -> Transformer:
    return Transformer.from_crs(CRS.from_proj4(RADOLAN_PROJ), CRS.from_epsg(4326), always_xy=True)


@lru_cache(maxsize=4)
def _origin(spec: GridSpec = DE1200) -> tuple[float, float]:
    """Projected coordinates of the lower-left corner of cell (0, 0)."""
    x_ref, y_ref = _to_radolan().transform(REF_LON, REF_LAT)
    return x_ref - spec.j_0, y_ref - spec.i_0


def cell_corners(spec: GridSpec = DE1200) -> tuple[np.ndarray, np.ndarray]:
    """Projected x (per column) and y (per row) of the lower-left cell corners, in km."""
    x0, y0 = _origin(spec)
    x = x0 + np.arange(spec.cols) * spec.res_km
    y = y0 + np.arange(spec.rows) * spec.res_km
    return x, y


@lru_cache(maxsize=2)
def outer_corners(spec: GridSpec = DE1200) -> dict[str, tuple[float, float]]:
    """WGS84 ``(lat, lon)`` of the grid's four outer corners, named as ODIM names them.

    What the HDF5 edition's ``where/LL_lat`` ... ``UR_lon`` state, so each cycle can be checked
    against the grid this module assumes (``radar/odim.py``).
    """
    x0, y0 = _origin(spec)
    x1, y1 = x0 + spec.cols * spec.res_km, y0 + spec.rows * spec.res_km
    out = {}
    for name, (x, y) in {"LL": (x0, y0), "LR": (x1, y0), "UL": (x0, y1), "UR": (x1, y1)}.items():
        lon, lat = _to_wgs84().transform(x, y)
        out[name] = (lat, lon)
    return out


def cell_of(lat: float, lon: float, spec: GridSpec = DE1200) -> tuple[int, int]:
    """Grid indices ``(row, col)`` containing the given WGS84 point.

    Raises :class:`OutsideGrid` for any coordinate that cannot be mapped, including NaN and
    infinities. These reach the service through the API: Python's ``json`` module accepts the bare
    tokens ``NaN`` and ``Infinity``, so a stored location can be non-finite without anyone trying.
    One such row, evaluated every cycle, would otherwise abort the run for every subscriber.
    """
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise OutsideGrid(f"lat={lat!r}, lon={lon!r} is not a finite coordinate")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise OutsideGrid(f"lat={lat!r}, lon={lon!r} is not a valid WGS84 coordinate")
    x, y = _to_radolan().transform(lon, lat)
    if not (math.isfinite(x) and math.isfinite(y)):
        raise OutsideGrid(f"lat={lat!r}, lon={lon!r} does not project onto the grid")
    x0, y0 = _origin(spec)
    col = math.floor((x - x0) / spec.res_km)
    row = math.floor((y - y0) / spec.res_km)
    if not (0 <= row < spec.rows and 0 <= col < spec.cols):
        raise OutsideGrid(f"lat={lat}, lon={lon} maps to row={row}, col={col}")
    return row, col


def cell_center(row: int, col: int, spec: GridSpec = DE1200) -> tuple[float, float]:
    """WGS84 ``(lat, lon)`` of the centre of a cell."""
    x0, y0 = _origin(spec)
    x = x0 + (col + 0.5) * spec.res_km
    y = y0 + (row + 0.5) * spec.res_km
    lon, lat = _to_wgs84().transform(x, y)
    return lat, lon


def radius_mask(
    lat: float, lon: float, radius_m: float, spec: GridSpec = DE1200
) -> tuple[np.ndarray, np.ndarray]:
    """Indices of every cell whose centre lies within ``radius_m`` of the point.

    Returns ``(rows, cols)`` index arrays, usable directly as ``values[rows, cols]``.

    Distances are true geodesic distances between the point and each cell centre, not distances in
    projected kilometres. The projection's scale factor at German latitudes is ~1.09, so projected
    kilometres are ~9 % longer than ground kilometres - taking the shortcut would quietly inflate
    every subscriber's radius.
    """
    if radius_m < 0:
        raise ValueError("radius_m must be >= 0")
    row, col = cell_of(lat, lon, spec)

    # Search a generous box: cells are ~1 km, the scale factor is <1.1, +2 covers rounding.
    reach = math.ceil(radius_m / 1000.0 / 0.9) + 2
    r_lo, r_hi = max(0, row - reach), min(spec.rows, row + reach + 1)
    c_lo, c_hi = max(0, col - reach), min(spec.cols, col + reach + 1)

    rr, cc = np.meshgrid(np.arange(r_lo, r_hi), np.arange(c_lo, c_hi), indexing="ij")
    x0, y0 = _origin(spec)
    x = x0 + (cc + 0.5) * spec.res_km
    y = y0 + (rr + 0.5) * spec.res_km
    lons, lats = _to_wgs84().transform(x, y)

    _, _, dist = _GEOD.inv(np.full(lons.shape, lon), np.full(lats.shape, lat), lons, lats)
    inside = dist <= radius_m
    if not inside.any():  # radius smaller than a cell: keep the containing cell
        return np.array([row]), np.array([col])
    return rr[inside], cc[inside]
