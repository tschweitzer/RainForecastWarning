"""Render a decoded RV frame as a PNG that Leaflet can lay over a map (DESIGN.md §11.1).

Why reproject at all: ``L.imageOverlay`` places an axis-aligned, unrotated image by lat/lng
bounds. The DE1200 grid is polar-stereographic, so drawing it directly would be visibly skewed -
rain would sit in the wrong place, which for this service is the whole ballgame.

Why nearest neighbour: the source is 1 km cells and the output is coarser than that. Interpolating
would invent precipitation between cells, and a smoothed edge is a worse lie than a blocky one.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer

from rainalert.radar.decoder import FINE_NONE, RVFrame
from rainalert.radar.grid import DE1200, RADOLAN_PROJ, GridSpec, cell_corners

#: Germany plus border areas, as (south, west), (north, east). Leaflet takes exactly this.
BOUNDS = ((46.0, 4.0), (55.9, 17.0))

#: Chosen so that one output pixel is never coarser than one source cell.
#:
#: The grid is 1 km. In Web Mercator a pixel covers more ground the further south it is, so the
#: binding case is the southern edge: at 46 N the image spans 1006 km, and 1120 px puts a pixel
#: at 0.90 km there and 0.72 km at the northern edge. Below about 1006 px the south would start
#: merging cells.
#:
#: It was 560 (1.5-1.8 km per pixel) until 2026-09-21, chosen for bandwidth. That halved the
#: linear resolution of the product this service exists to show, and combined with point
#: sampling it meant two thirds of the wet cells in a frame were never drawn at all.
WIDTH = 1120

#: How many source rows to project at once. Small enough that the float64 intermediates stay a
#: few MB, large enough that pyproj is still called in useful batches rather than per row.
_PROJECTION_BAND_ROWS = 100

#: The rain-intensity bands: the one place the scale is defined.
#:
#: Each entry is (mm per 5 min at which the band starts, RGBA, German name). Below the first
#: stop the pixel is fully transparent, so "no rain" and "no data" both read as "nothing drawn" -
#: the map is not the place to distinguish them; the staleness banner and the gap markers are.
#:
#: The map legend, the overlay renderer and the threshold picker on the settings page all read
#: this, so a colour on the map and a colour in the dropdown mean the same rain by construction
#: rather than by two people remembering to edit two lists.
#:
#: **The names.** Rain intensity is conventionally classified in mm per *hour*, and RV measures
#: mm per five-minute interval, so the hourly figure in each comment is the 5-minute value times
#: twelve - i.e. "if it kept raining this hard for an hour". That is the usual way radar
#: intensities are labelled and it is still an extrapolation, not a measurement: a shower that
#: rains 6 mm in five minutes and then stops did not deliver 72 mm.
#:
#: The boundaries are the ones already chosen for the map, and the names are the conventional
#: classes those hourly rates fall into (light / moderate / heavy, with DWD's Starkregen warning
#: thresholds - 15-25 mm/h markant, 25-40 mm/h Unwetter - landing in the top three bands).
#: **The alpha here is the final one.** It used to be multiplied again by the Leaflet layer's
#: own opacity - 0.75 on the map, 0.6 on the settings page - which put the lightest band at an
#: effective 0.38 and 0.31: light blue at a third strength over a basemap, which is close to
#: invisible. Two opacities multiplying is also a bad way to reason about a palette, so the
#: layer is now drawn at 1.0 and this column is what you actually see.
INTENSITY_BANDS: tuple[tuple[float, tuple[int, int, int, int], str], ...] = (
    # The noise floor: below 0.008 mm nothing is drawn and nothing can warn (D-80). Chosen by the
    # operator with the debug noise cut (D-79) on DWD's own thousandths (D-78), which show the
    # faint fields under it to be mostly not rain. History: 0.05 (~0.6 mm/h) until 2026-10-07,
    # which hid two thirds of the wet cells and drizzle that was falling (D-52); then 0.01, the
    # product's quantum, which - because every detected cell is stored as at least 0.01 - drew
    # and warned on every echo the radar saw, noise included.
    (0.008, (120, 180, 255, 140), "Nieselregen"),  # ~0.1 mm/h
    (0.15, (60, 140, 240, 173), "leichter Regen"),  # ~1.8 mm/h
    (0.35, (40, 190, 150, 199), "mäßiger Regen"),  # ~4.2 mm/h
    (0.70, (245, 210, 70, 217), "kräftiger Regen"),  # ~8.4 mm/h
    (1.50, (240, 140, 45, 230), "starker Regen"),  # ~18 mm/h
    (3.00, (225, 60, 60, 240), "Starkregen"),  # ~36 mm/h
    (6.00, (170, 40, 140, 247), "extremer Starkregen"),  # ~72 mm/h
)

#: What the Leaflet layers draw at. A constant rather than a number typed into two templates,
#: because the whole point of the change above is that there is one opacity, not two.
LAYER_OPACITY = 1.0

#: The lowest intensity the map draws anything at. Anything below this is real rain that is
#: deliberately not rendered, which is exactly the distinction the ingest log needs to make when
#: it reports how wet a cycle was - otherwise "0 wet cells" and "nothing visible" look like the
#: same fact when they are not.
DRAWN_FROM_MM_5MIN: float = INTENSITY_BANDS[0][0]

#: Where the colour gradient is anchored (D-60): the drizzle band's own colour is drawn at 0.01,
#: as it was before the floor moved below it (D-80), so nothing visible changed from 0.01 up
#: and every drizzle reading keeps a shade of its own. 0.008 and 0.009 get that colour too.
GRADIENT_FROM: float = 0.01

#: What the renderer wants: just the thresholds and their colours.
COLOR_STOPS: tuple[tuple[float, tuple[int, int, int, int]], ...] = tuple(
    (threshold, colour) for threshold, colour, _ in INTENSITY_BANDS
)

#: mm per 5 min -> mm per hour, for labelling only. See the note above on what it does not mean.
INTERVALS_PER_HOUR = 12


@dataclass(frozen=True)
class Projection:
    """How source cells and output pixels correspond, in both directions.

    Two mappings, because neither alone is enough.

    ``rows``/``cols`` is the **inverse** map: for each output pixel, the source cell its centre
    falls in. It guarantees every pixel gets a value, which matters wherever the output is finer
    than the source - otherwise the picture would have holes.

    ``scatter_*`` is the **forward** map: for each source cell, the output pixel it lands in,
    pre-sorted into groups so a frame can be max-reduced per pixel in one pass. It guarantees
    every source cell is accounted for, which the inverse map does not: with 1 km cells and
    pixels of a similar size, sampling one cell per pixel silently discarded most of them.
    """

    rows: np.ndarray
    cols: np.ndarray
    inside: np.ndarray
    width: int
    height: int
    #: Flat source indices, ordered so that cells sharing an output pixel are adjacent.
    scatter_source: np.ndarray
    #: Where each output pixel's run begins in ``scatter_source``.
    scatter_starts: np.ndarray
    #: The flat output pixel each run belongs to.
    scatter_pixels: np.ndarray


def build_projection(spec: GridSpec = DE1200, width: int = WIDTH) -> Projection:
    """Map every output pixel to a source cell.

    Computed the straightforward way, per render pass rather than per frame. If it ever shows up in
    ``rainalert_pipeline_seconds``, cache it then - not before (§11.1).
    """
    (south, west), (north, east) = BOUNDS
    to_mercator = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_epsg(3857), always_xy=True)
    x0, y0 = to_mercator.transform(west, south)
    x1, y1 = to_mercator.transform(east, north)
    # Keep the aspect ratio honest in Mercator, so the picture is not stretched.
    height = round(width * (y1 - y0) / (x1 - x0))

    px = x0 + (np.arange(width) + 0.5) * (x1 - x0) / width
    # Image rows run north to south; Mercator y runs south to north.
    py = y1 - (np.arange(height) + 0.5) * (y1 - y0) / height
    mesh_x, mesh_y = np.meshgrid(px, py)

    to_wgs84 = Transformer.from_crs(CRS.from_epsg(3857), CRS.from_epsg(4326), always_xy=True)
    lon, lat = to_wgs84.transform(mesh_x, mesh_y)

    to_radolan = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_proj4(RADOLAN_PROJ), always_xy=True
    )
    gx, gy = to_radolan.transform(lon, lat)

    xs, ys = cell_corners(spec)
    cols = np.floor((gx - xs[0]) / spec.res_km).astype(np.int64)
    rows = np.floor((gy - ys[0]) / spec.res_km).astype(np.int64)
    inside = (rows >= 0) & (rows < spec.rows) & (cols >= 0) & (cols < spec.cols)

    # Narrow before anything else keeps a reference: a 1120x1361 index array is 12.2 MB as
    # int64 and 3.0 MB as int16, and the largest index either of these can hold is 1199.
    rows = np.clip(rows, 0, spec.rows - 1).astype(np.int16)
    cols = np.clip(cols, 0, spec.cols - 1).astype(np.int16)
    del gx, gy, lon, lat, mesh_x, mesh_y

    # The other direction: where does each source cell land? Cell centres, not corners, so a
    # cell is attributed to the pixel containing its middle rather than its lower-left edge.
    #
    # Built a band of rows at a time. Doing it whole needs a dozen 1200x1100 float64
    # intermediates at once - meshgrids, two transforms, the floors - which is ~135 MB of peak
    # that the allocator then hangs on to. On a 1 GB machine that peak is the difference
    # between a job that runs and a box that stops responding.
    to_wgs84_from_radolan = Transformer.from_crs(
        CRS.from_proj4(RADOLAN_PROJ), CRS.from_epsg(4326), always_xy=True
    )
    centres_x = xs + spec.res_km / 2
    targets: list[np.ndarray] = []
    sources: list[np.ndarray] = []
    for start in range(0, spec.rows, _PROJECTION_BAND_ROWS):
        stop = min(start + _PROJECTION_BAND_ROWS, spec.rows)
        band_x, band_y = np.meshgrid(centres_x, ys[start:stop] + spec.res_km / 2)
        band_lon, band_lat = to_wgs84_from_radolan.transform(band_x, band_y)
        merc_x, merc_y = to_mercator.transform(band_lon, band_lat)
        target_col = np.floor((merc_x - x0) / (x1 - x0) * width).astype(np.int64)
        # y is flipped for the same reason as above: image rows run north to south.
        target_row = np.floor((y1 - merc_y) / (y1 - y0) * height).astype(np.int64)
        landed = (
            (target_row >= 0) & (target_row < height) & (target_col >= 0) & (target_col < width)
        )
        targets.append((target_row * width + target_col)[landed].astype(np.int32))
        sources.append((np.flatnonzero(landed.ravel()) + start * spec.cols).astype(np.int32))

    target = np.concatenate(targets)
    source = np.concatenate(sources)
    del targets, sources

    # Sorted once, here, so that rendering a frame is a reduceat over contiguous runs rather
    # than a scattered np.maximum.at - which is the same arithmetic and about eight times
    # slower, measured, because it cannot vectorise over repeated indices.
    order = np.argsort(target, kind="stable")
    target, source = target[order], source[order]
    del order
    starts = np.flatnonzero(np.r_[True, np.diff(target) != 0]).astype(np.int32)

    return Projection(rows, cols, inside, width, height, source, starts, target[starts])


#: How many shades each band is drawn in (D-60). Not a continuous blend: every distinct colour
#: costs PNG compression, and a continuous gradient made a wet frame 282 KB instead of 77 KB - on
#: a map that loads a frame every five minutes of the window, over mobile data. 32 per band is
#: 7 x 32 + 1 = 225 colours, which still fits a PNG palette (at most 256; one byte a pixel instead
#: of four), and it is the smallest count at which every one of `Nieselregen`'s fourteen possible
#: readings gets a shade of its own (16 gave 11 of 14). Measured on a wet frame: 107 KB.
SHADES_PER_BAND = 32

_STOP_VALUES = np.array(
    [GRADIENT_FROM, *(threshold for threshold, _ in COLOR_STOPS[1:])], dtype=np.float64
)
_STOP_LOGS = np.log(_STOP_VALUES)
_STOP_COLOURS = np.array([colour for _, colour in COLOR_STOPS], dtype=np.float64)


def _palette() -> np.ndarray:
    """The RGBA of every palette index: 0 is transparent, then each band's shades in order.

    Shade `s` of band `b` is the colour `s / SHADES_PER_BAND` of the way from band `b`'s colour to
    the next band's, so shade 0 is exactly the band's own colour. The last band has no next one
    and keeps its colour in every shade.
    """
    table = np.zeros((1 + len(COLOR_STOPS) * SHADES_PER_BAND, 4), dtype=np.float64)
    for band in range(len(COLOR_STOPS)):
        nxt = min(band + 1, len(COLOR_STOPS) - 1)
        for shade in range(SHADES_PER_BAND):
            t = shade / SHADES_PER_BAND
            table[1 + band * SHADES_PER_BAND + shade] = _STOP_COLOURS[band] + t * (
                _STOP_COLOURS[nxt] - _STOP_COLOURS[band]
            )
    return np.rint(table).astype(np.uint8)


#: The readings below 0.02 mm that the HDF5 edition carries to the thousandth (D-78): 0.000 to
#: 0.019, which `values` stores as 0.01. Each gets a palette entry of its own after the bands.
FINE_STEPS = 20
FINE_BASE = 1 + len(COLOR_STOPS) * SHADES_PER_BAND
#: How many of them lie below the noise floor (D-80): 0.000 to 0.007.
BELOW_FLOOR_STEPS = round(INTENSITY_BANDS[0][0] * 1000)


def _shown_alpha(k: int) -> int:
    """The alpha fine reading `k` (thousandths) is drawn with - or would be, below the floor."""
    return INTENSITY_BANDS[0][1][3] - 9 + k


def _fine_colours() -> np.ndarray:
    """The drizzle band's own colour, changed just enough to be told apart by a program.

    Alpha runs 131..150 around the band's 140 - under four percent of opacity either way, which
    nobody sees on a pale blue, and in the direction of the reading, so a heavier one is never
    the fainter. Red is one above the band's so that none of them, the 140 included, is the
    colour that already means "0.01" (index 1) - which frames without thousandths still draw.

    Below the noise floor (D-80) the entry is transparent - not drawn - but keeps a colour of its
    own (green 172-179, which nothing else has): the debug noise cut (noisecut.js, D-79) gives such
    an entry its alpha back, from `value_table`'s `below`, to show what the floor removes.
    """
    r, g, b, _ = INTENSITY_BANDS[0][1]
    return np.array(
        [
            (r + 1, g - BELOW_FLOOR_STEPS + k, b, 0)
            if k < BELOW_FLOOR_STEPS
            else (r + 1, g, b, _shown_alpha(k))
            for k in range(FINE_STEPS)
        ],
        dtype=np.uint8,
    )


#: Index -> RGBA. Built once; `render_frame` writes it into the PNG as its palette. 225 band shades
#: and the 20 fine drizzle readings: 245 of the 256 a palette PNG may have.
PALETTE = np.concatenate([_palette(), _fine_colours()])


def palette_index(values: np.ndarray, fine: np.ndarray | None = None) -> np.ndarray:
    """Values in mm/5min to palette indices: 0 for NaN and anything below the noise floor.

    ``fine``, where given: per pixel, DWD's reading in thousandths for a pixel drawn at 0.01, NaN
    elsewhere (`sample_fine`). Such a pixel then gets the fine entry for its reading instead of
    the band's first shade (D-78) - a transparent one below the floor (D-80). Every other pixel is
    unaffected.

    **A gradient, not seven flat steps** (DESIGN.md D-60). Each band's own colour is drawn exactly
    at the value where the band starts - so the legend and the threshold picker, which show those
    colours, still name what you see - and a value between two band starts is shaded toward the
    next band's colour by how far along it is. Flat steps hid most of what the radar says: since
    D-52 `Nieselregen` alone runs from 0.01 to 0.15 mm/5 min, fourteen distinct readings, and all
    fourteen were the same pale blue.

    How far along is measured on a **log** scale, because that is how the bands are spaced
    (0.01, 0.15, 0.35, 0.7, 1.5, 3, 6 - each step roughly doubling) and how rain is felt: 0.02 is
    twice 0.01, while 0.14 against 0.13 is the same rain. Linearly, the first band's 0.01-0.07
    would all sit in its palest third. Above the last band start the colour stays the last one.

    Interpolated in sRGB, alpha included. The stops were chosen as neighbours (blue, blue, teal,
    yellow, orange, red, violet), so the straight line between two of them passes through no
    muddy middle that a perceptual colour space would be needed to avoid.
    """
    v = np.asarray(values, dtype=np.float64)
    # Snapped to well below the product's 0.01 quantum first. The decoder keeps readings as
    # float32, where 0.35 is 0.3499999940 - and a reading of exactly a band's start must get
    # exactly that band's colour, not the end of the band below (the trap D-52 fixed in the
    # sampler).
    v = np.round(np.nan_to_num(v, nan=-1.0), 6)

    index = np.zeros(v.shape, dtype=np.uint8)
    drawn = v >= DRAWN_FROM_MM_5MIN
    if not drawn.any():
        return index
    # 0.008 and 0.009 are shaded as 0.01, where the gradient starts (GRADIENT_FROM).
    shaded = np.maximum(v[drawn], GRADIENT_FROM)
    b = np.searchsorted(_STOP_VALUES, shaded, side="right") - 1
    last = len(_STOP_VALUES) - 1
    nxt = np.minimum(b + 1, last)
    with np.errstate(divide="ignore", invalid="ignore"):
        fraction = np.where(
            b < last, (np.log(shaded) - _STOP_LOGS[b]) / (_STOP_LOGS[nxt] - _STOP_LOGS[b]), 0.0
        )
    shade = np.clip(np.floor(fraction * SHADES_PER_BAND), 0, SHADES_PER_BAND - 1).astype(np.int64)
    index[drawn] = (1 + b * SHADES_PER_BAND + shade).astype(np.uint8)
    if fine is not None:
        refine = (index == 1) & np.isfinite(fine)
        index[refine] = (FINE_BASE + np.clip(fine[refine], 0, FINE_STEPS - 1)).astype(np.uint8)
    return index


def colorize(values: np.ndarray) -> np.ndarray:
    """Values in mm/5min to an RGBA image array - the colours `render_frame` draws.

    NaN and anything below the first band are transparent. See `palette_index` for the scale.
    """
    return PALETTE[palette_index(values)]


def render_frame(frame: RVFrame, projection: Projection | None = None) -> bytes:
    """One frame to PNG bytes, losing no wet cell above the noise floor. What each pixel holds is
    `sample_frame`'s, and for the pixels drawn at 0.01, `sample_fine`'s (D-78, D-80)."""
    projection = projection or build_projection()
    sampled = sample_frame(frame, projection)
    readings = sample_fine(frame, projection)
    # A palette PNG: one byte a pixel and the colour table once, with each entry's alpha in the
    # tRNS chunk. Browsers decode it to exactly the RGBA `colorize` gives (D-60), and the map's
    # rain tooltip reads the indices back out of it (rainprobe.js, D-77).
    image = Image.fromarray(palette_index(sampled, readings), mode="P")
    image.putpalette(PALETTE[:, :3].ravel().tolist())
    buffer = io.BytesIO()
    # optimize=True costs a little CPU per frame and saves rather more bandwidth on 168 of them.
    image.save(buffer, format="PNG", optimize=True, transparency=PALETTE[:, 3].tobytes())
    return buffer.getvalue()


def sample_frame(frame: RVFrame, projection: Projection | None = None) -> np.ndarray:
    """The value, in mm/5min, each output pixel shows; NaN where nothing is drawn.

    Each output pixel shows the **heaviest** rain among the source cells that fall in it. That
    is the same rule the alerting uses over a subscriber's radius (sampler.sample), and for the
    same reason: a pixel one of whose cells is under a shower is a pixel where you get wet, and
    averaging would dilute exactly the small convective cells this service exists to catch.

    It is also the difference between a picture and a sample of one. Taking one cell per pixel -
    which is what this did until 2026-09-21 - left two thirds of a frame's wet cells undrawn and
    could miss the heaviest cell in the country entirely.
    """
    projection = projection or build_projection()
    return _sample(np.where(frame.missing, np.nan, frame.values), projection)


def sample_fine(frame: RVFrame, projection: Projection | None = None) -> np.ndarray | None:
    """Per pixel, the heaviest fine reading (thousandths, D-78) among its cells; NaN where none.

    The same rule as `sample_frame`, on the same cells, so a pixel drawn at 0.01 - every one of
    whose cells is detected and below 0.02 - gets the heaviest of exactly those cells' readings.
    None for a frame without fine readings (the binary edition).
    """
    if frame.fine is None:
        return None
    projection = projection or build_projection()
    readings = np.where(frame.fine == FINE_NONE, np.nan, frame.fine.astype(np.float32))
    return _sample(readings, projection)


def _sample(values: np.ndarray, projection: Projection) -> np.ndarray:
    """A grid of values (NaN for none) onto the output pixels: the heaviest cell per pixel."""
    # Start from the inverse map, so every pixel has a value even where the output is finer
    # than the source. NaN means "nothing to draw", which colorize renders transparent.
    sampled = values[projection.rows, projection.cols]
    sampled[~projection.inside] = np.nan

    # Then raise each pixel to the maximum of the cells that actually landed in it. -1 stands in
    # for missing so that a real reading always wins over no reading, and so that a pixel whose
    # cells are all missing keeps the NaN it started with rather than becoming 0 mm.
    readings = np.maximum.reduceat(
        np.nan_to_num(values.ravel()[projection.scatter_source], nan=-1.0),
        projection.scatter_starts,
    )
    flat = sampled.ravel()
    current = np.nan_to_num(flat[projection.scatter_pixels], nan=-1.0)
    combined = np.maximum(current, readings)
    flat[projection.scatter_pixels] = np.where(combined < 0, np.nan, combined)
    return flat.reshape(projection.height, projection.width)


def legend() -> list[dict]:
    """The colour scale, so the page can draw a legend without hard-coding it twice."""
    return [
        {
            "from_mm_5min": threshold,
            "rgba": list(colour),
            "label": label,
            "approx_mm_per_hour": round(threshold * INTERVALS_PER_HOUR, 1),
        }
        for threshold, colour, label in INTENSITY_BANDS
    ]


#: The resolution RV values come in since D-74: whole hundredths of a millimetre.
_QUANTUM = 0.01


@lru_cache(maxsize=1)
def value_table() -> dict:
    """What every colour an overlay can contain stands for, so the map can say it (D-77).

    `rows` has one entry per colour, `[r, g, b, a, from, to, band]`: the smallest and largest
    reading drawn in that colour in mm per 5 min - `to` is None for the top band, one colour for
    everything from its start up - and an index into `bands`, the band names. Rows rather than
    objects because this rides in every timeline manifest: 162 colours as objects were 16 KB.

    `below`, the same shape, is the fine readings under the noise floor (D-80): in a frame their
    colour has alpha 0, and the `a` here is the one the debug noise cut gives them back (D-79).
    `floor` is the floor itself, the lowest reading that is drawn and can warn.

    Built by running every reading the product can carry through `palette_index` - not by
    inverting the formula - so the table cannot disagree with the renderer. Keyed by colour rather
    than palette index because colour is what the file guarantees: an encoder that renumbered the
    palette would still draw the same RGBA.
    """
    top = INTENSITY_BANDS[-1][0]
    readings = np.arange(1, round(top / _QUANTUM) + 1) * _QUANTUM
    indices = palette_index(readings)
    rows: dict[int, list] = {}
    # The fine drizzle readings from the floor up first, each its own colour and exactly one value
    # (D-78). The band's first shade keeps its row below: frames without thousandths - from the
    # binary edition, or rendered before D-78 - still draw it.
    for k in range(BELOW_FLOOR_STEPS, FINE_STEPS):
        index = FINE_BASE + k
        rows[index] = [*(int(c) for c in PALETTE[index]), k / 1000, k / 1000, 0]
    for value, index in zip(readings.tolist(), indices.tolist(), strict=True):
        if index == 0:
            continue
        value = round(value, 2)
        if index in rows:
            rows[index][5] = value
        else:
            band = (index - 1) // SHADES_PER_BAND
            rows[index] = [*(int(c) for c in PALETTE[index]), value, value, band]
    # The top band starts at `top` and has no end: everything heavier is drawn the same.
    rows[int(palette_index(np.array([top]))[0])][5] = None
    below = [
        [*(int(c) for c in PALETTE[FINE_BASE + k][:3]), _shown_alpha(k), k / 1000, k / 1000, 0]
        for k in range(BELOW_FLOOR_STEPS)
    ]
    return {
        "bands": [label for _, _, label in INTENSITY_BANDS],
        "floor": DRAWN_FROM_MM_5MIN,
        "rows": [rows[index] for index in sorted(rows)],
        "below": below,
    }
