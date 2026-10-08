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
    PALETTE,
    SHADES_PER_BAND,
    WIDTH,
    build_projection,
    colorize,
    palette_index,
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
    """How strong a value is drawn: its palette index, 0 for nothing drawn.

    The index is ordered by rain - band by band, shade by shade (D-60) - so "drawn at least as
    strongly" is a comparison of indices. Finer than the seven bands this used to compare: a cell
    drawn one shade too light now fails too.
    """
    return palette_index(values).astype(np.int64)


def rendered_bands(frame, projection) -> np.ndarray:
    """The palette index each output pixel shows, read back out of the PNG."""
    image = Image.open(io.BytesIO(render_frame(frame, projection)))
    assert image.mode == "P", "a palette PNG, D-60"
    return np.array(image).ravel().astype(np.int64)


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
    drawable = source > 0
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
    drawable = group_max > 0
    assert (shown[drawable] == group_max[drawable]).all()


def test_the_heaviest_cell_in_the_frame_reaches_the_picture(frame, projection):
    """It did not, before: the peak fell between sample points and was simply absent."""
    values = np.where(frame.missing, np.nan, frame.values)
    heaviest = band_of(values).max()
    assert heaviest > 0
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


def test_every_band_starts_where_it_says_and_the_first_at_the_quantum():
    """A cell decoded at exactly a band's start is drawn in that band, from the lowest step up.

    The decoder stores `raw * 0.01` as float32, which cannot hold most hundredths (35 is
    0.3499999940). Under NumPy's promotion rules the threshold is cast to float32 too, so the
    comparison happens to agree - this pins that it keeps agreeing, through the decoder's own
    arithmetic rather than a hand-typed float. And it pins D-52: the first band starts at the
    product's quantum, so no non-zero reading is left off the map.
    """
    from rainalert.radar.overlay import colorize

    raws = [round(threshold * 100) for threshold, _ in COLOR_STOPS]
    assert raws[0] == 1, "the lowest band must start at the smallest step RV reports (D-52)"

    values = (np.array(raws, dtype=np.uint16) * 0.01).astype(np.float32)  # as decode_frame
    drawn = colorize(values)
    for index, (raw, (_, colour)) in enumerate(zip(raws, COLOR_STOPS, strict=True)):
        assert tuple(drawn[index]) == colour, (
            f"a reading of exactly {raw / 100} drew the wrong band"
        )

    assert colorize(np.array([0.0], dtype=np.float32))[0, 3] == 0, "dry must stay transparent"


#: Every reading RV can express up to 10 mm/5 min, made the way the decoder makes them.
READINGS = (np.arange(0, 1001, dtype=np.uint16) * 0.01).astype(np.float32)


def test_more_rain_is_never_drawn_lighter():
    """D-60: the shades are ordered - a heavier reading never gets an earlier palette entry."""
    index = palette_index(READINGS)
    assert (np.diff(index.astype(np.int64)) >= 0).all()
    assert index[0] == 0, "dry stays transparent"


def test_nieselregen_is_no_longer_one_colour():
    """The point of D-60. Its fourteen readings (0.01-0.14) were all the same pale blue."""
    drizzle = READINGS[(READINGS >= 0.0099) & (READINGS < 0.1499)]
    assert len(drizzle) == 14
    colours = {tuple(c) for c in colorize(drizzle)}
    assert len(colours) == 14, f"only {len(colours)} shades for fourteen readings"


def test_the_shades_fit_a_png_palette():
    """More than 256 colours and the frame is a full-colour PNG again - four bytes a pixel."""
    assert len(PALETTE) <= 256


def test_every_shade_lies_between_its_band_colour_and_the_next():
    """The legend still names what is drawn: a pixel's colour is on the way from its band's colour
    to the next band's, never somewhere else."""
    stops = np.array([c for _, c in COLOR_STOPS], dtype=np.float64)
    for band in range(len(COLOR_STOPS)):
        nxt = min(band + 1, len(COLOR_STOPS) - 1)
        for shade in range(SHADES_PER_BAND):
            colour = PALETTE[1 + band * SHADES_PER_BAND + shade].astype(np.float64)
            low, high = np.minimum(stops[band], stops[nxt]), np.maximum(stops[band], stops[nxt])
            assert ((colour >= low - 0.5) & (colour <= high + 0.5)).all(), (band, shade)


def test_the_png_shows_exactly_the_colours_colorize_names(frame, projection):
    """The PNG is a palette image with per-entry alpha; decoded, it must be colorize's RGBA."""
    image = np.array(Image.open(io.BytesIO(render_frame(frame, projection))).convert("RGBA"))
    used = np.unique(image.reshape(-1, 4), axis=0)
    palette = {tuple(c) for c in PALETTE[1:]} | {(0, 0, 0, 0)}
    for colour in used:
        rgba = tuple(int(c) for c in colour)
        assert rgba in palette or rgba[3] == 0, rgba
