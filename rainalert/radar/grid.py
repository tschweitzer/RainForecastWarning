"""Georeferencing for the DE1200 RADOLAN grid.

DE1200 is a polar-stereographic grid on a sphere (R = 6370040 m), 1200 rows x 1100 columns of
nominally 1 km cells. Its definition is pinned here as constants and verified against
``wradlib.georef.get_radolan_grid`` in the tests - wradlib is the oracle, not a runtime dependency
(D-21).

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

#: The RADOLAN projection. Sphere, not an ellipsoid - this is DWD's definition, not a choice.
RADOLAN_PROJ = (
    "+proj=stere +lat_0=90 +lat_ts=60 +lon_0=10 +x_0=0 +y_0=0 +R=6370040 +units=km +no_defs"
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
    """The coordinate falls outside the DE1200 rectangle.

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


def cell_of(lat: float, lon: float, spec: GridSpec = DE1200) -> tuple[int, int]:
    """Grid indices ``(row, col)`` containing the given WGS84 point."""
    x, y = _to_radolan().transform(lon, lat)
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
