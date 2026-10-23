"""Turn a decoded cycle into one series of numbers per subscription (DESIGN.md §8).

Two properties this module exists to guarantee:

* **Indexed by lead, never by position** (§8.0). A cycle missing one member would otherwise shift
  every later value down a slot, so rain at +60 gets announced as rain at +55 and the wrong time is
  written to the permanent record.
* **Per-frame coverage.** The no-data region advects with the forecast, so a point near the edge of
  radar range can have good data at t+0 and none at t+45. A single coverage number taken from
  frame 0 would silently evaluate garbage for exactly those subscribers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from rainalert.radar.decoder import FINE_NONE, RVFrame
from rainalert.radar.grid import radius_mask

#: Leads a complete RV cycle carries: 0, 5, ... 120.
LEADS = tuple(range(0, 125, 5))
SLOTS = len(LEADS)


@dataclass(frozen=True)
class SampleSeries:
    """What one location saw across one cycle. ``None`` means "that lead was not present"."""

    max_rate_by_lead: tuple[float | None, ...]
    missing_fraction: tuple[float | None, ...]

    @staticmethod
    def slot(lead_minutes: int) -> int:
        return lead_minutes // 5

    def at(self, lead_minutes: int) -> tuple[float | None, float | None]:
        index = self.slot(lead_minutes)
        return self.max_rate_by_lead[index], self.missing_fraction[index]


class MaskCache:
    """Radius masks keyed by ``(row, col, radius_m)``.

    Subscribers in the same town share a mask, so the cost of sampling is bounded by the number of
    *distinct* masks rather than the number of subscribers - and distinct masks are bounded by the
    grid. That is the whole scale story; see §8.1 before optimising further.
    """

    def __init__(self) -> None:
        self._masks: dict[tuple[float, float, int], tuple[np.ndarray, np.ndarray]] = {}

    def get(self, lat: float, lon: float, radius_m: int) -> tuple[np.ndarray, np.ndarray]:
        # Round the coordinate to the grid before keying, so two neighbours in one cell share.
        key = (round(lat, 3), round(lon, 3), radius_m)
        if key not in self._masks:
            self._masks[key] = radius_mask(lat, lon, radius_m)
        return self._masks[key]

    def __len__(self) -> int:
        return len(self._masks)


#: The thousandths `RVFrame.fine` holds (D-78).
FINE_PRECISION = 0.001


def on_the_grid(value: float, precision: float) -> float:
    """Snap a decoded reading back to the decimal the radar actually reported.

    RV values are integers times ``precision`` (``raw * 0.01``), but the decoder keeps them as
    float32, which cannot hold most hundredths: 35 comes back as 0.3499999940, 1 as 0.0099999998.
    Thresholds arrive as the doubles of the same decimals (0.35, 0.01), so comparing the two
    directly decided that a reading of exactly 0.35 is below 0.35. Rounding to the precision's
    decimal places gives the double nearest the reported decimal, which is the very double the
    threshold parses to - so "exactly at the threshold" compares as equal, as it should.
    """
    return round(value, round(-math.log10(precision)))


def sample(
    frames: list[RVFrame], lat: float, lon: float, radius_m: int, cache: MaskCache | None = None
) -> SampleSeries:
    """Aggregate the radius around one point, for every lead the cycle actually contains.

    ``max`` rather than ``mean``: a 2 km radius one of whose cells is under a shower means the
    subscriber gets wet. A mean would dilute exactly the small convective cells this service exists
    to catch.

    **Below 0.02 mm the reading is DWD's thousandth, not the stored 0.01** (D-80). `values` keeps
    every detected cell at 0.01 or more, so a threshold under 0.02 could not tell an echo of
    0.000 from drizzle of 0.019; `RVFrame.fine` can. From 0.02 up nothing changes. A frame without
    thousandths (the binary edition) is read as before.
    """
    # `cache or MaskCache()` would be wrong: MaskCache defines __len__, so an *empty* cache is
    # falsy and the shared one would be silently replaced on every call - making the whole
    # dedupe-by-mask property in §8.1 quietly untrue.
    if cache is None:
        cache = MaskCache()
    rows, cols = cache.get(lat, lon, radius_m)

    rates: list[float | None] = [None] * SLOTS
    missing: list[float | None] = [None] * SLOTS
    for frame in frames:
        if frame.lead_minutes % 5 or not 0 <= frame.lead_minutes <= LEADS[-1]:
            continue  # not a lead this product should carry; the ingest gate already flagged it
        index = frame.lead_minutes // 5
        window_missing = frame.missing[rows, cols]
        missing[index] = float(window_missing.mean())
        if window_missing.all():
            rates[index] = None  # no data is not zero rain
            continue
        window = frame.values[rows, cols]
        precision = frame.precision
        if frame.fine is not None:
            fine = frame.fine[rows, cols]
            window = np.where(fine != FINE_NONE, fine * FINE_PRECISION, window)
            precision = FINE_PRECISION
        rates[index] = on_the_grid(float(np.nanmax(window)), precision)
    return SampleSeries(tuple(rates), tuple(missing))
