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

#: Half the native resolution: ~2 km per pixel, which is plenty for a phone and keeps each frame
#: to a couple of hundred kilobytes. 168 of these get loaded over mobile data (§11.1).
WIDTH = 560

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
INTENSITY_BANDS: tuple[tuple[float, tuple[int, int, int, int], str], ...] = (
    (0.05, (120, 180, 255, 130), "Nieselregen"),  # ~0.6 mm/h
    (0.15, (60, 140, 240, 170), "leichter Regen"),  # ~1.8 mm/h
    (0.35, (40, 190, 150, 190), "mäßiger Regen"),  # ~4.2 mm/h
    (0.70, (245, 210, 70, 205), "kräftiger Regen"),  # ~8.4 mm/h
    (1.50, (240, 140, 45, 220), "starker Regen"),  # ~18 mm/h
    (3.00, (225, 60, 60, 235), "Starkregen"),  # ~36 mm/h
    (6.00, (170, 40, 140, 245), "extremer Starkregen"),  # ~72 mm/h
)

#: What the renderer wants: just the thresholds and their colours.
COLOR_STOPS: tuple[tuple[float, tuple[int, int, int, int]], ...] = tuple(
    (threshold, colour) for threshold, colour, _ in INTENSITY_BANDS
)

#: mm per 5 min -> mm per hour, for labelling only. See the note above on what it does not mean.
INTERVALS_PER_HOUR = 12


@dataclass(frozen=True)
class Projection:
    """The source pixel each output pixel takes its value from."""

    rows: np.ndarray
    cols: np.ndarray
    inside: np.ndarray
    width: int
    height: int


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
    return Projection(
        np.clip(rows, 0, spec.rows - 1), np.clip(cols, 0, spec.cols - 1), inside, width, height
    )


def colorize(values: np.ndarray) -> np.ndarray:
    """Values in mm/5min to an RGBA image array. NaN and sub-threshold are transparent."""
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    for threshold, colour in COLOR_STOPS:
        rgba[np.nan_to_num(values, nan=-1.0) >= threshold] = colour
    return rgba


def render_frame(frame: RVFrame, projection: Projection | None = None) -> bytes:
    """One frame to PNG bytes."""
    projection = projection or build_projection()
    values = np.where(frame.missing, np.nan, frame.values)
    sampled = values[projection.rows, projection.cols]
    sampled[~projection.inside] = np.nan
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
