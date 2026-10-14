"""Is this radar cycle believable? (DESIGN.md D-70)

Replaced the blast radius, which judged a cycle by how many subscribers it would notify at once.
That only means something when subscribers are a fair sample of the country: with five of them in
one town, "half of them at once" is ordinary weather, and with ten thousand in one city a single
front can legitimately warn hundreds. Either way it dropped valid warnings, and for good, because
the states had already moved on.

This judges the data instead. Rain areas grow and move gradually - the largest 5-minute change in
the wet share of the radar area measured on 31 hours of production frames was 0.69 percentage
points, and a radar site coming back online in rain adds about 5 - while a corrupt "rain
everywhere" cycle jumps by 85 to 95. A cycle that jumps too far is set aside like one with missing
data: nothing changes, nothing is sent, and the next good cycle evaluates normally. Real rain costs
at most five minutes; a broken cycle costs nothing.

Only rises count. A corrupt "dry everywhere" cycle sends nothing, and one dry cycle moves nobody's
state far enough to matter (a warning needs three misses to retract, rain thirty dry minutes to
clear).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

#: What counts as wet: the lowest band the map draws (radar/overlay.py), the product's quantum.
WET_MM_5MIN = 0.01


def wet_share(values: np.ndarray, missing: np.ndarray) -> float | None:
    """Percent of the cells with data that are wet. None when no cell has data."""
    valid = ~missing & np.isfinite(values)
    count = int(valid.sum())
    if count == 0:
        return None
    return 100.0 * float((values[valid] >= WET_MM_5MIN).sum()) / count


def implausible(
    now_share: float | None,
    lead_shares: Sequence[float | None],
    previous_share: float | None,
    limit_pp: float,
) -> str | None:
    """Why this cycle cannot be believed, or None.

    `previous_share` is the previous cycle's share at t+0, or None when there is no recent one -
    after a gap rain can genuinely have spread, so only the forecast is compared then.
    """
    if not limit_pp or now_share is None:
        return None
    if previous_share is not None and now_share - previous_share > limit_pp:
        return (
            f"wet share at t+0 rose from {previous_share:.1f}% to {now_share:.1f}% "
            f"in one cycle (limit {limit_pp:g} pp)"
        )
    wettest = max((s for s in lead_shares if s is not None), default=None)
    if wettest is not None and wettest - now_share > limit_pp:
        return (
            f"a forecast frame is {wettest:.1f}% wet against {now_share:.1f}% at t+0 "
            f"(limit {limit_pp:g} pp)"
        )
    return None
