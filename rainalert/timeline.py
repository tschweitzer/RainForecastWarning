"""The −12 h … +2 h manifest that drives the map slider (DESIGN.md §11.1, D-22).

Two kinds of frame, both from RV:

* **observed** (offset <= 0): the t+0 analysis frame of each past cycle - what the radar saw.
* **forecast** (offset > 0): leads 5…120 of the *latest* cycle only.

They are asymmetric on purpose - past frames each come from their own cycle, future frames all come
from one - and the manifest labels them so the page can too. A user must never read a forecast
frame as an observation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rainalert.config import Settings
from rainalert.db.models import CycleStatus, RadarCycle
from rainalert.radar.overlay import BOUNDS, legend
from rainalert.storage import OverlayStore

CYCLE_MINUTES = 5


@dataclass(frozen=True)
class Frame:
    offset_minutes: int  # negative for the past
    valid_time: str
    kind: str  # observed | forecast
    source_cycle: str
    url: str


@dataclass(frozen=True)
class Gap:
    """A cycle we never got. Rendered as a hole, not as the previous image."""

    from_offset_minutes: int
    to_offset_minutes: int


def build_timeline(
    session: Session,
    settings: Settings,
    store: OverlayStore,
    past_hours: int | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    # Clamped at both ends. The upper bound stops a caller asking for a window the store cannot
    # serve; the lower one matters because `past_hours or default` lets a negative through, and a
    # window that starts after it ends returns an empty map rather than an error - which reads as
    # "the radar is down" (§4.3.1: input from outside is checked, not trusted).
    past_hours = min(past_hours or settings.timeline_past_hours, settings.timeline_past_hours)
    past_hours = max(past_hours, 1)

    latest = session.execute(
        select(RadarCycle)
        .where(RadarCycle.status.in_([CycleStatus.OK, CycleStatus.PARTIAL]))
        .order_by(RadarCycle.nominal_time.desc())
        .limit(1)
    ).scalar_one_or_none()

    if latest is None:
        return {
            "now": now.isoformat(),
            "latest_cycle": None,
            "stale": True,
            "bounds": [list(BOUNDS[0]), list(BOUNDS[1])],
            "frames": [],
            "gaps": [],
            "colorscale": legend(),
            "attribution": "Deutscher Wetterdienst (DWD), Radarprodukt RV, CC BY 4.0",
        }

    anchor = latest.nominal_time
    window_start = anchor - timedelta(hours=past_hours)
    cycles = (
        session.execute(
            select(RadarCycle)
            .where(
                RadarCycle.nominal_time >= window_start,
                RadarCycle.nominal_time <= anchor,
                RadarCycle.status.in_([CycleStatus.OK, CycleStatus.PARTIAL]),
            )
            .order_by(RadarCycle.nominal_time)
        )
        .scalars()
        .all()
    )
    have = {cycle.nominal_time for cycle in cycles}

    frames: list[Frame] = []
    for cycle in cycles:
        offset = round((cycle.nominal_time - anchor).total_seconds() / 60)
        frames.append(
            Frame(
                offset_minutes=offset,
                valid_time=cycle.nominal_time.isoformat(),
                kind="observed",
                source_cycle=cycle.nominal_time.isoformat(),
                url=store.url_for_observed(cycle.nominal_time),
            )
        )

    for lead in range(CYCLE_MINUTES, 125, CYCLE_MINUTES):
        frames.append(
            Frame(
                offset_minutes=lead,
                valid_time=(anchor + timedelta(minutes=lead)).isoformat(),
                kind="forecast",
                source_cycle=anchor.isoformat(),
                url=store.url_for_forecast(anchor, lead),
            )
        )

    # Every 5-minute slot in the window that produced no cycle. The client draws these as gaps;
    # holding the previous image would fake continuity across a radar outage.
    gaps: list[Gap] = []
    slot = window_start
    while slot <= anchor:
        if slot not in have:
            offset = round((slot - anchor).total_seconds() / 60)
            if gaps and gaps[-1].to_offset_minutes == offset - CYCLE_MINUTES:
                gaps[-1] = Gap(gaps[-1].from_offset_minutes, offset)
            else:
                gaps.append(Gap(offset, offset))
        slot += timedelta(minutes=CYCLE_MINUTES)

    age_minutes = (now - anchor).total_seconds() / 60
    return {
        "now": now.isoformat(),
        "latest_cycle": anchor.isoformat(),
        "age_minutes": round(age_minutes, 1),
        # The page says so plainly rather than showing old rain as if it were current.
        "stale": age_minutes > settings.timeline_stale_after_minutes,
        "bounds": [list(BOUNDS[0]), list(BOUNDS[1])],
        "frames": [asdict(frame) for frame in frames],
        "gaps": [asdict(gap) for gap in gaps],
        "colorscale": legend(),
        "attribution": "Deutscher Wetterdienst (DWD), Radarprodukt RV, CC BY 4.0",
    }
