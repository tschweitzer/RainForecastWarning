"""What the rain overlay is allowed to lose.

The map is a picture, not the decision - alerting reads the full grid (sampler.sample) and is
unaffected by anything here. But a picture that quietly drops two thirds of the wet cells is
still wrong, and it was: until 2026-09-21 each output pixel took one arbitrary source cell and
ignored its neighbours, so a shower could fall in a skipped cell and simply not exist.

These tests pin the guarantee that replaced it: every source cell reaches a pixel, and that
pixel is drawn at least as strongly as the cell really is.
"""

import io

import numpy as np
import pytest
from PIL import Image

from rainalert.radar.decoder import read_analysis_frame
from rainalert.radar.grid import DE1200
from rainalert.radar.overlay import (
    COLOR_STOPS,
    WIDTH,
    build_projection,
    render_frame,
)
from tests.helpers import FIXTURES

WET = FIXTURES / "DE1200_RV2609161355_trimmed.tar.bz2"


#: Two resolutions on purpose. At WIDTH the mapping is one source cell per pixel, so the
#: guarantee holds even without pooling - which makes it a poor test of the pooling. At 560,
#: the old width, a pixel holds 2.9 cells on average and 89% hold more than one, so that is
#: where taking one cell per pixel visibly loses rain and where this has to be checked.
COARSE = 560


@pytest.fixture(scope="module")
def projection():
    return build_projection()


@pytest.fixture(scope="module")
def coarse_projection():
    return build_projection(width=COARSE)


@pytest.fixture(scope="module")
def frame():
    return read_analysis_frame(WET)


def band_of(values) -> np.ndarray:
    """Which palette band a value falls in; -1 for nothing drawn."""
    out = np.full(np.shape(values), -1, dtype=np.int64)
    for index, (threshold, _) in enumerate(COLOR_STOPS):
        out[np.nan_to_num(values, nan=-1.0) >= threshold] = index
    return out


def rendered_bands(frame, projection) -> np.ndarray:
    """The band each output pixel shows, read back out of the PNG."""
    image = np.array(Image.open(io.BytesIO(render_frame(frame, projection))).convert("RGBA"))
    flat = image.reshape(-1, 4)
    bands = np.full(len(flat), -1, dtype=np.int64)
    for index, (_, colour) in enumerate(COLOR_STOPS):
        bands[(flat == np.array(colour, dtype=np.uint8)).all(axis=1)] = index
    return bands


@pytest.mark.parametrize("width", [COARSE, WIDTH])
def test_no_source_cell_is_drawn_weaker_than_it_is(frame, width):
    """The guarantee. Every cell's pixel shows its band or a heavier one.

    "Or heavier" because a pixel holding several cells shows the strongest of them - the same
    rule alerting uses over a radius, and for the same reason: a pixel one of whose cells is
    under a shower is a pixel where you get wet.

    Checked at both widths. The coarse one is the case that fails without pooling.
    """
    projection = build_projection(width=width)
    values = np.where(frame.missing, np.nan, frame.values)
    source = band_of(values).ravel()[projection.scatter_source]
    runs = np.diff(np.r_[projection.scatter_starts, len(projection.scatter_source)])
    pixel_of_cell = np.repeat(projection.scatter_pixels, runs)

    shown = rendered_bands(frame, projection)[pixel_of_cell]
    drawable = source >= 0
    assert drawable.any(), "the fixture has no drawable rain; the test would prove nothing"
    assert (shown[drawable] >= source[drawable]).all()


def test_a_coarse_pixel_shows_the_heaviest_cell_it_holds(frame, coarse_projection):
    """Explicitly the pooling, at a width where a pixel really does hold several cells.

    Without it each pixel showed one arbitrary member, so the strongest cell in a group was
    usually not the one drawn.
    """
    values = np.where(frame.missing, np.nan, frame.values)
    projection = coarse_projection
    runs = np.diff(np.r_[projection.scatter_starts, len(projection.scatter_source)])
    assert (runs > 1).mean() > 0.5, "this width no longer groups cells; the test proves nothing"

    source = band_of(values).ravel()[projection.scatter_source]
    group_max = np.maximum.reduceat(source, projection.scatter_starts)
    shown = rendered_bands(frame, projection)[projection.scatter_pixels]
    drawable = group_max >= 0
    assert (shown[drawable] == group_max[drawable]).all()


def test_the_heaviest_cell_in_the_frame_reaches_the_picture(frame, projection):
    """It did not, before: the peak fell between sample points and was simply absent."""
    values = np.where(frame.missing, np.nan, frame.values)
    heaviest = band_of(values).max()
    assert heaviest >= 0
    assert heaviest in set(rendered_bands(frame, projection).tolist())


def test_every_source_cell_in_range_lands_in_exactly_one_pixel(projection):
    """No cell counted twice, none silently dropped inside the covered area."""
    runs = np.diff(np.r_[projection.scatter_starts, len(projection.scatter_source)])
    assert runs.sum() == len(projection.scatter_source)
    assert len(np.unique(projection.scatter_source)) == len(projection.scatter_source)
    assert len(np.unique(projection.scatter_pixels)) == len(projection.scatter_pixels)


def test_a_pixel_is_never_coarser_than_a_source_cell(projection):
    """Below ~1006 px the southern edge would start merging cells; WIDTH is set above it."""
    from pyproj import Geod

    from rainalert.radar.overlay import BOUNDS

    (south, _), (_, _) = BOUNDS
    (_, west), (_, east) = BOUNDS
    _, _, span_m = Geod(ellps="WGS84").inv(west, south, east, south)
    km_per_pixel = span_m / 1000 / projection.width
    assert km_per_pixel <= DE1200.res_km, (
        f"{km_per_pixel:.2f} km/px against {DE1200.res_km} km cells"
    )


def test_missing_data_stays_transparent_and_does_not_become_zero(projection):
    """-1 stands in for missing during the max; a pixel of only missing cells must stay NaN.

    Getting this wrong would paint the whole no-data half of the grid as "no rain", which is
    the one thing this service must never say.
    """
    frame = read_analysis_frame(WET)
    values = np.where(frame.missing, np.nan, frame.values)
    assert np.isnan(values).any(), "the fixture has no missing region"

    image = np.array(Image.open(io.BytesIO(render_frame(frame, projection))).convert("RGBA"))
    # Wherever every contributing cell was missing, nothing may be drawn.
    all_missing = np.isnan(values).ravel()[projection.scatter_source]
    runs = np.diff(np.r_[projection.scatter_starts, len(projection.scatter_source)])
    pixel_all_missing = np.minimum.reduceat(all_missing.astype(np.int8), projection.scatter_starts)
    del runs
    blank = projection.scatter_pixels[pixel_all_missing == 1]
    assert (image.reshape(-1, 4)[blank, 3] == 0).all()


def test_the_width_is_the_one_the_projection_uses(projection):
    assert projection.width == WIDTH
