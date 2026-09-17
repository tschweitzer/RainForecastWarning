"""Prometheus-format metrics (DESIGN.md §15).

Computed on request from the database rather than kept in a process-local registry. That is the
right shape here specifically because the service scales to zero and runs the ingest work in a
*separate* process: an in-memory counter in the API would never see a single cycle the job ingested.
The queries are trivial at this scale.

The key SLI is deliberately defensive. `cycle_age_seconds` derives from a timestamp the remote party
supplies, so a cycle stamped in the future would read as "the freshest data we ever had" and silence
the staleness alert until real time caught up. It is clamped at zero, and a negative raw age is
exported separately as its own alertable condition (SECURITY_REVIEW.md F-7).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rainalert.config import Settings
from rainalert.db.models import (
    CycleStatus,
    Notification,
    RadarCycle,
    Subscription,
    SubscriptionStatus,
)


def _line(name: str, value: float, help_text: str, kind: str = "gauge") -> str:
    return f"# HELP {name} {help_text}\n# TYPE {name} {kind}\n{name} {value}\n"


def render(session: Session, settings: Settings, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    out: list[str] = []

    latest = session.execute(
        select(RadarCycle.nominal_time).order_by(RadarCycle.nominal_time.desc()).limit(1)
    ).scalar_one_or_none()

    if latest is None:
        raw_age = None
    else:
        raw_age = (now - latest).total_seconds()

    out.append(
        _line(
            "rainalert_cycle_age_seconds",
            max(0.0, raw_age) if raw_age is not None else -1,
            "Age of the newest radar cycle. -1 means none at all. Alert above 1200.",
        )
    )
    out.append(
        _line(
            "rainalert_cycle_age_negative",
            1 if (raw_age is not None and raw_age < 0) else 0,
            "1 when the newest cycle is stamped in the future, which would silence the staleness "
            "alert. Its own paging condition.",
        )
    )

    for status in CycleStatus:
        count = session.execute(
            select(func.count())
            .select_from(RadarCycle)
            .where(RadarCycle.status == status, RadarCycle.fetched_at >= now - timedelta(hours=24))
        ).scalar_one()
        out.append(f'rainalert_cycles_24h{{status="{status.value}"}} {count}\n')

    for status in SubscriptionStatus:
        count = session.execute(
            select(func.count()).select_from(Subscription).where(Subscription.status == status)
        ).scalar_one()
        out.append(f'rainalert_subscriptions{{status="{status.value}"}} {count}\n')

    for status in ("queued", "sent", "failed", "expired"):
        count = session.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.status == status, Notification.queued_at >= now - timedelta(hours=24)
            )
        ).scalar_one()
        out.append(f'rainalert_notifications_24h{{status="{status}"}} {count}\n')

    # Cycles missing from the timeline window: a non-zero value means the map shows holes.
    window_start = now - timedelta(hours=settings.timeline_past_hours)
    have = session.execute(
        select(func.count())
        .select_from(RadarCycle)
        .where(RadarCycle.nominal_time >= window_start, RadarCycle.nominal_time <= now)
    ).scalar_one()
    expected = int(settings.timeline_past_hours * 60 / 5)
    out.append(
        _line(
            "rainalert_timeline_gaps",
            max(0, expected - have),
            "Cycles missing from the map's history window.",
        )
    )
    return "".join(out)
