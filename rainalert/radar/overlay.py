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

import numpy as np
from PIL import Image
from pyproj import CRS, Transformer

from rainalert.radar.decoder import RVFrame
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
    (0.05, (120, 180, 255, 140), "Nieselregen"),  # ~0.6 mm/h
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


def colorize(values: np.ndarray) -> np.ndarray:
    """Values in mm/5min to an RGBA image array. NaN and sub-threshold are transparent."""
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    for threshold, colour in COLOR_STOPS:
        rgba[np.nan_to_num(values, nan=-1.0) >= threshold] = colour
    return rgba


def render_frame(frame: RVFrame, projection: Projection | None = None) -> bytes:
    """One frame to PNG bytes, losing no wet cell.

    Each output pixel shows the **heaviest** rain among the source cells that fall in it. That
    is the same rule the alerting uses over a subscriber's radius (sampler.sample), and for the
    same reason: a pixel one of whose cells is under a shower is a pixel where you get wet, and
    averaging would dilute exactly the small convective cells this service exists to catch.

    It is also the difference between a picture and a sample of one. Taking one cell per pixel -
    which is what this did until 2026-09-21 - left two thirds of a frame's wet cells undrawn and
    could miss the heaviest cell in the country entirely.
    """
    projection = projection or build_projection()
    values = np.where(frame.missing, np.nan, frame.values)

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
    sampled = flat.reshape(projection.height, projection.width)

    image = Image.fromarray(colorize(sampled), mode="RGBA")
    buffer = io.BytesIO()
    # optimize=True costs a little CPU per frame and saves rather more bandwidth on 168 of them.
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


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
