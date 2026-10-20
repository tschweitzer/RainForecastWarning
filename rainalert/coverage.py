"""Saying why there is no warning when the radar cannot see a subscriber's location (D-75).

A cycle with too little data at a location changes nothing for it - no warning, no all-clear
(`state_machine.advance`, step 0). That is right, and it is silent: the reader sees a settings page
that says nothing is wrong. This says it, and names the radar when one that covers the location is
reported missing, so "Radar Hannover liefert gerade keine Daten" reads as the cause it is rather
than as the service being broken.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rainalert.db.models import Evaluation, RadarCycle, Subscription
from rainalert.radar.sites import responsible

#: Only about the present: three cycles. Older than that the map's stale banner is the message.
RECENT = timedelta(minutes=15)


def coverage_note(
    session: Session, subscription: Subscription, now: datetime | None = None
) -> str | None:
    """A sentence for the settings page, or None when the location had data last cycle."""
    now = now or datetime.now(UTC)
    latest = session.execute(
        select(Evaluation.decision, Evaluation.evaluated_at, RadarCycle.missing_sites)
        .join(RadarCycle, RadarCycle.id == Evaluation.cycle_id)
        .where(Evaluation.subscription_id == subscription.id)
        .order_by(Evaluation.evaluated_at.desc())
        .limit(1)
    ).first()
    if latest is None or latest.decision != "skipped_missing" or latest.evaluated_at < now - RECENT:
        return None
    site = responsible(subscription.lat, subscription.lon, latest.missing_sites or ())
    if site is not None:
        return (
            f"An deinem Ort fehlen gerade Radardaten: das Radar {site.name} liefert keine Daten. "
            "Solange können wir dich hier nicht warnen."
        )
    return "An deinem Ort fehlen gerade Radardaten. Solange können wir dich hier nicht warnen."
