"""Georeferencing tests.

An offset of a few cells here is invisible in every other test and warns the wrong village, so the
grid is compared against wradlib over *all* 1.32 M cells rather than at a handful of points.
"""

import numpy as np
import pytest

from rainalert.radar.grid import (
    DE1200,
    OutsideGrid,
    cell_center,
    cell_corners,
    cell_of,
    radius_mask,
)


@pytest.mark.golden
def test_whole_grid_matches_wradlib():
    wradlib = pytest.importorskip("wradlib")
    reference = wradlib.georef.get_radolan_grid(DE1200.rows, DE1200.cols, wgs84=True)

    from pyproj import CRS, Transformer

    from rainalert.radar.grid import RADOLAN_PROJ

    x, y = cell_corners()
    xx, yy = np.meshgrid(x, y)
    to_wgs84 = Transformer.from_crs(
        CRS.from_proj4(RADOLAN_PROJ), CRS.from_epsg(4326), always_xy=True
    )
    lon, lat = to_wgs84.transform(xx, yy)

    assert np.abs(lon - reference[..., 0]).max() < 1e-9
    assert np.abs(lat - reference[..., 1]).max() < 1e-9


def test_row_zero_is_the_southern_edge():
    south_lat, _ = cell_center(0, DE1200.cols // 2)
    north_lat, _ = cell_center(DE1200.rows - 1, DE1200.cols // 2)
    assert south_lat < north_lat


@pytest.mark.parametrize(
    ("name", "lat", "lon"),
    [
        ("Frankfurt", 50.1109, 8.6821),
        ("Hamburg", 53.5511, 9.9937),
        ("Munich", 48.1351, 11.5820),
        ("Borkum", 53.5800, 6.6600),
    ],
)
def test_cell_contains_its_point(name, lat, lon):
    """The returned cell must actually contain the point, to within half a cell diagonal."""
    row, col = cell_of(lat, lon)
    clat, clon = cell_center(row, col)
    assert abs(clat - lat) < 0.010, name
    assert abs(clon - lon) < 0.016, name


def test_cell_of_and_cell_center_round_trip():
    rng = np.random.default_rng(20260916)
    for row, col in zip(
        rng.integers(0, DE1200.rows, 500), rng.integers(0, DE1200.cols, 500), strict=True
    ):
        lat, lon = cell_center(int(row), int(col))
        assert cell_of(lat, lon) == (int(row), int(col))


def test_points_outside_the_grid_raise():
    for lat, lon in [(35.0, 10.0), (60.0, 10.0), (51.0, -20.0), (51.0, 40.0)]:
        with pytest.raises(OutsideGrid):
            cell_of(lat, lon)


def test_radius_mask_is_a_disc_of_the_expected_size():
    lat, lon = 50.1109, 8.6821
    rows, cols = radius_mask(lat, lon, 2000)
    assert 9 <= len(rows) <= 21  # a ~2 km disc on a ~1 km grid
    assert len(rows) == len(cols)
    assert (row_col := (cell_of(lat, lon))) in set(zip(rows.tolist(), cols.tolist(), strict=True))
    assert row_col is not None


def test_radius_mask_uses_ground_distance_not_projected_km():
    """Projected km are ~9 % longer than ground km here; the shortcut inflates the radius."""
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    lat, lon = 50.1109, 8.6821
    rows, cols = radius_mask(lat, lon, 5000)
    for row, col in zip(rows.tolist(), cols.tolist(), strict=True):
        clat, clon = cell_center(row, col)
        assert geod.inv(lon, lat, clon, clat)[2] <= 5000.0


def test_radius_mask_grows_monotonically():
    lat, lon = 50.1109, 8.6821
    sizes = [len(radius_mask(lat, lon, r)[0]) for r in (0, 1000, 2000, 5000, 10000)]
    assert sizes == sorted(sizes)
    assert sizes[0] == 1  # a zero radius still yields the containing cell


@pytest.mark.parametrize(
    ("lat", "lon", "label"),
    [
        (float("nan"), 8.0, "NaN latitude"),
        (51.0, float("nan"), "NaN longitude"),
        (float("inf"), 8.0, "infinite latitude"),
        (51.0, float("-inf"), "infinite longitude"),
        (91.0, 8.0, "latitude out of WGS84 range"),
        (51.0, 200.0, "longitude out of WGS84 range"),
    ],
)
def test_non_finite_and_out_of_range_coordinates_raise_outside_grid(lat, lon, label):
    """Only OutsideGrid, never ValueError/OverflowError (F-3).

    These are reachable through the API - json.loads accepts the bare token NaN - and one such
    stored row is re-evaluated every cycle, so anything that escapes here is a permanent outage
    for every subscriber, not just for the bad row.
    """
    with pytest.raises(OutsideGrid):
        cell_of(lat, lon)
    with pytest.raises(OutsideGrid):
        radius_mask(lat, lon, 2000)
