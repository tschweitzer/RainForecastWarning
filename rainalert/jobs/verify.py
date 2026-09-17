"""Did the rain we warned about actually arrive? (DESIGN.md §9)

Cheap, and the only honest way to tune the defaults in D-13 later. Without it, "is 30 minutes the
right lead time" is a matter of opinion; with it, it is a hit rate and a false-alarm ratio.

Runs over the evaluation window while both the events and the archived grids are still around.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rainalert.db.models import Evaluation, RainEvent

logger = logging.getLogger(__name__)

#: How close the observed onset must be to count the warning as correct.
TOLERANCE = timedelta(minutes=15)


@dataclass
class VerificationReport:
    judged: int = 0
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.judged if self.judged else None


def verify_events(
    session: Session, now: datetime | None = None, tolerance: timedelta = TOLERANCE
) -> VerificationReport:
    """Judge every event whose predicted time has passed and which has not been judged yet."""
    now = now or datetime.now(UTC)
    report = VerificationReport()

    events = (
        session.execute(
            select(RainEvent).where(
                RainEvent.verified.is_(None),
                RainEvent.predicted_start_at < now - tolerance,
            )
        )
        .scalars()
        .all()
    )

    for event in events:
        # Did t+0 ever report rain at this location within the tolerance window?
        window_start = event.predicted_start_at - tolerance
        window_end = event.predicted_start_at + tolerance
        observed = (
            session.execute(
                select(Evaluation)
                .where(
                    Evaluation.subscription_id == event.subscription_id,
                    Evaluation.now_wet.is_(True),
                    Evaluation.evaluated_at >= window_start,
                    Evaluation.evaluated_at <= window_end,
                )
                .order_by(Evaluation.evaluated_at)
                .limit(1)
            )
            .scalars()
            .first()
        )
        if observed is not None:
            event.observed_start_at = observed.evaluated_at
            event.verified = True
            report.hits += 1
        else:
            # Only judge as a miss once the evidence window has fully passed; otherwise a warning
            # made minutes ago would be scored before the rain had a chance to arrive.
            if now < window_end:
                continue
            event.verified = False
            report.misses += 1
        report.judged += 1

    session.commit()
    if report.judged:
        logger.info(
            "verified %d event(s): %d hit, %d missed (hit rate %.0f%%)",
            report.judged,
            report.hits,
            report.misses,
            100 * (report.hit_rate or 0),
        )
    return report
